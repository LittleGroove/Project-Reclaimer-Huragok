# Managing a running server

Setting a server up is in [setup.md](setup.md). This is what the window and the
commands do with a server that's running: its status card, its console, everything
about it changed live, bans and admins, updates of the server program and of its
Workshop mods, cleaning up, and a console for moderators who aren't at this PC.

## A server's status card: V
**V** picks a server and shows, on one screen, everything the Superintendent keeps an
eye on:

| | |
| --- | --- |
| Running | its state and uptime, RCON's reply time, how fresh its status file is, its process and the memory it and its game use; a job waiting for its next start |
| Joining | the address players join at, listed or hidden, join password or open, players and the limit, ping limit, the welcome lines |
| What it plays | its playlist (name, games, vote seconds) or game, and the game under way |
| Program | its Project Reclaimer version, whether it updates itself (auto_update) or Huragok does, the newest release |
| Mods | its Workshop items: installed version, Steam's newest, a new version waiting; how they're updated; the SteamCMD account; what they take on disk; mod sharing; character mods |
| Health | the watchdog's state and why, the starts, restarts and crashes of the last 24 hours with their reasons, failed starts, FAULTED and why |
| Safety | anti-cheat, the VPN block and its lists, admins, bans (and whether the ban list is shared) |
| Settings | voice channels, text chat, votes, friendly fire, team changing, live reload |
| Scheduled | autostart, the daily restart, a planned restart, Huragok at boot |
| Problems and notes | its configuration errors, a failed settings reload, an expired SteamCMD sign-in, a mod update waiting |

From the card: **D** its settings, **G** what it plays, **R** restart it, **L** watch
its console, each with the server already picked; **U** the updates screen and **C**
clean up, which are for every server, not only this one; **F** looks again. A second
window shows the card too, from the files and the supervising window's live line.

The dashboard's line under each server carries the important part: the watchdog's
reason (`watchdog: degraded (slow in 3 of the last 5 minutes (longest frame 412
ms))`, `watchdog: critical (4 players connected in 5 minutes and none got in)`),
`2 restart(s)/crash(es) in 24 h`, `mod update waiting`, and in red (starting `!`)
what needs you: a settings reload that failed, a SteamCMD sign-in that expired, a
stop that couldn't end the server's process (`! couldn't stop it: …`), a program
or Workshop update that keeps failing, until it's fixed.

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

## Everything about a server: D
**D** shows a server's settings in groups, each with how many it holds and its most
telling values, then the group's settings with their values, where each comes from
(this server, `[defaults]`, the file, a table, settings.toml, or the program's own
default) and what it does:

1. **Joining**: join password, hidden, max/min players, ping limit, load timeout,
   welcome lines, enabled
2. **What it plays**: a playlist (picked from its playlists), one map and game type
   (picked from what it can play; with a base map for a Forge map on a mod map), a
   saved custom game, **campaign co-op** (the mission by name, rally point,
   difficulty, custom characters, and the campaign modifiers: enemy health and damage,
   and from Project Reclaimer 0.9.19 sprint, its speed and stamina), or **Halo 3:
   ODST** (0.9.19): a campaign mission (from a numbered list, with where it starts,
   the difficulty and the same modifiers) or Firefight (one of ten maps, one of ODST's
   Firefight game types, the difficulty), picked from what `dedicated content odst`
   lists for the server. An ODST server needs ODST's files in its game folder (about
   3.5 GB; `dedicated init --from --maps odst`): when they're missing, D offers to
   add them from MCC (hard links on the same drive take no space; ODST must be
   installed in MCC: Steam, MCC, Properties, DLC). A campaign or ODST server holds
   16 players at most and won't start with more, so D offers to bring `max_players`
   down first. Switching kind takes the other kind's settings out; a playlist applies
   from the next vote, a change of kind once nobody is on. Modifiers D doesn't ask
   about (`skulls`, a number the program's guide doesn't explain yet) are kept as
   you wrote them
3. **Gameplay**: friendly fire, team changing, the camera (0.9.20: `first_person`
   for everyone, the default, which also puts third-person character mods in first
   person; `player_choice`, each player switching with P; or `third_person` for
   everyone; multiplayer only), votes, trust player games
4. **Voice and chat**: the voice channels, text chat, chat log
5. **Safety and admins**: anti-cheat, the VPN block and its lists, admins for every
   server in the folder and this server's own
6. **Mods and the Workshop**: mod sharing and its upload limit, character mods,
   Workshop items, SteamCMD's program and account
7. **Updates**: auto_update, the server's Workshop update checks, and how its new
   Workshop versions are installed (Huragok's own)
8. **Listing and network**: host names, port, public address, UPnP, stats server,
   the discovery master (on, port, peers, origin)
9. **Remote console**: its password and address, this server's own port and password
10. **Folders and files**: game files, content, data, the ban list (each checked to be
    there), live reload
11. **Huragok's own**: autostart, its own daily restart time (settings.toml: a time,
    emptied for no daily restart on this server, or `-` to go back to the
    `[restarts]` one)

A choice is picked from a numbered list (team changing: 1 balanced, 2 game_type,
3 off), never typed. For a typed value, **Enter leaves it as it was**; emptying a
value that has one asks first, and the console password can't be emptied here
(Huragok needs the console). Passwords show only as "set (12 characters)", show as
stars while you type them, and go into `logs/window.log` as `********`.

**D refuses a value the server program would refuse**, before anything is written:
min players above max players, more than 16 players on a campaign or ODST server,
a host name over 48 characters (or with control characters in it), a stats
server, public address, master origin or console address in the wrong form, a
port the discovery master or any server here uses (disabled and left-out ones
too), a rally point past the mission's last. It also refuses **a setting the
server's program is too old for**, judged by the version it runs (or else its
program file's own `--version`): `camera needs Project Reclaimer 0.9.20 or newer
(this server has 0.9.18)`. The camera needs 0.9.20; ODST, campaign sprint and
skulls 0.9.19; `trust_player_games`, the customization settings, `no_vanilla_characters`
and `auto_reload` 0.9.18. (Decision: refused rather than written with a warning,
since a program that meets a setting it doesn't know won't start.)

A change is written in place (`dedicated.toml` keeps its comments; Huragok's own go
into `settings.toml` and are in force at once). Every write is checked first: the
change is made in memory and read back, and the file is replaced only if exactly
that one change differs; otherwise it's left byte for byte as it was and the screen
says `couldn't change … safely …: edit it by hand (E)`. A setting written in a form
the editor won't touch (a dotted key such as `rcon.password = …`, an inline table
such as `rcon = { … }`) is refused the same way. Since Project Reclaimer 0.9.18 the
server re-reads its file a couple of seconds later, so no restart is needed for most
of them. The screen says, for each, when it applies:

| Applies | Settings |
| --- | --- |
| at once | join password, max/min players, max ping, load timeout, votes, voice (global, team, proximity), text chat, welcome lines, mod sharing, host name, campaign's trust player games, the folder's admins |
| from the next game | friendly fire, team changing, the camera (a campaign or ODST server restarts once empty for friendly fire) |
| from the next vote | the playlist |
| the server restarts by itself once empty | hidden, port, anti-cheat, VPN block, chat log, character mods, enabled, stats server, UPnP, the console password and address, anything about a campaign or ODST server's game |
| when `dedicated` restarts | auto_update, the Workshop update checks (`update_check_minutes`), SteamCMD's program and account, the VPN lists, public address, the discovery master, the game, content and data folders |

For a setting that applies when `dedicated` restarts, D offers to restart the
server: **after this game**, **when nobody's on**, **now** (with the usual
countdown) or **later** (at its next restart).

Welcome lines: one line, or several separated by `|` (up to 5). A setting the
server sets in its own `[[server]]` block can be taken out again (`-`, or **D** on
an on/off setting): the `[defaults]` value, or the program's own default, applies
again. `admins` takes player IDs (64 hexadecimal characters, from the game's
Settings > Online services > Your player ID).

The dashboard's events show the server's answer: `settings reloaded: max_players
16 → 12`, or `settings NOT reloaded, nothing changed` with what's wrong (which then
stays in red on the server's line until a reload goes through). Three things change
live over the console instead of the file, until a restart: `servername`, `password`
(join) and `maxping`.

Two things on D need the server stopped (X), and say so otherwise (a server
waiting to be started again after a crash counts as running: "the Superintendent
is about to restart it (backoff): stop it first (X)"):
- **rename this server**: its name everywhere it's used: its `[[server]]` block, its
  block in `settings.toml`, its data folder (logs, chat, status), Huragok's record of
  it and its console log, and its folder in `SERVERS/` when that's named after it.
  The folder is renamed first, and if any later step fails every step is put back
  ("Every step was put back: it's still Alpha"). A new name that would give the
  same file name as another server's ("LG Slayer" beside "LG-Slayer") is refused;
- **its own content folder** (shown when it shares one): a copy of the shared
  `content/` in its own folder, hard-linked on the same drive so no space is used,
  and its `dedicated.toml` pointed at it. What Huragok writes into it later
  (imports, Forge layouts, playlists, custom games, vote images) replaces the file
  rather than writing through the link, so the shared copy never changes with it.

## Bans, admins and the VPN block: B
**B** on a running server: the ban list (`bans`), `unban`, a ban of someone who
isn't connected (player ID, address or range, with a time and a reason), `pardon`
(let a kicked player back in now), the admins (`admin list`, `admin add`, `admin
remove`: on every server sharing the ban list, no restart), and the VPN block
(`vpn`, `vpn <address>`, `vpnallow`, `vpnrevoke`). Everything goes through the
console with your name on it. On a stopped server it shows the ban list from its
folder (`dedicated bans`).

## Updates: U
**U** offers four things:

1. **Huragok** itself, from its GitHub releases: the servers keep running
   while it updates. While a new version waits, the dashboard's HURAGOK line says
   `Huragok <version> is out (U)`; with `[updates] auto_update = true` it installs
   itself as soon as it's found instead (off unless you turn it on). The install is renames only, all or nothing (if a file can't
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
2. **The server program** (Project Reclaimer), from its releases. Players must be
   on exactly the server's version, and releases have come out daily, so a server
   left behind empties out. Each server's `dedicated.toml` decides how it keeps up
   (D switches it; the dashboard header says `server program 0.9.18 · 2 of 3 update
   themselves`, and `check` says it for each server):
   - **`auto_update = true`: the server updates itself.** It looks at the releases
     when it starts and every hour, checks the download against the release's
     SHA-256 list, and restarts its game on the new version at once, mid-game; it's
     back within seconds, and the process Huragok holds stays up through it. The log
     says `PARDON OUR DUST! Project Reclaimer installed a new release by itself`, then
     `now on Project Reclaimer <version>`. With `[server_updates] follow = true` (the
     default) the other self-updating servers then restart after their game: they
     look for releases as they start, so the whole fleet is on the new version
     within a game rather than within the hour. A self-updating server needs a copy
     of the program of its own, in a folder it can write to: `check` says when it
     hasn't. New servers update themselves (`[setup] auto_update`).

     **Huragok makes sure it happened.** It looks at the releases hourly too, and
     reads what each server's console says about its own updater. When a server
     says `Automatic update failed; retrying in an hour: …` (or that its update feed
     is off), or is still on an older version 70 minutes after the release came out
     and after it had its look, Huragok installs the release for it instead: it
     downloads and checks it once (`records/program-download/`), restarts the server
     **after its game**, and puts the new program in place of that server's copy
     while it's stopped (the old one goes to `records/previous-program/`). The log
     says why (`[Alpha] its own update failed (…): Huragok installs Project Reclaimer
     0.9.21 after its game`), the server's line and its card (V) say what's waiting.
     If the server updates itself in the meantime, Huragok's restart is called off.
     `[server_updates] fallback = false` only says so.

     A program older than its `dedicated.toml` can't start at all: it reads its
     settings before it looks for releases, and stops at one it doesn't know
     (0.9.15 at 0.9.20's `auto_reload`), so it can't update itself either. When a
     start fails like that, Huragok asks the program its version (`--version`): if
     it's older than the newest release, Huragok puts the newest in place before the
     next try (`it won't start: Project Reclaimer 0.9.15 is older than its
     dedicated.toml (it doesn't know `auto_reload`)…`); if it's the newest already,
     the log says that line of the file is a mistake. With auto_update off it only
     says what to do.

     After a self-update the process Huragok holds still runs from the old file
     (`project-reclaimer.exe.old`), which the next update might not be able to
     replace, so Huragok restarts it on the new file and removes the old one: once
     nobody is on, or after 30 minutes with the usual countdown warnings.
   - **`auto_update = false`: Huragok tells you.** Every hour it looks at the
     releases and compares with the version each running server reports (`server
     program 0.9.18 (0.9.19 out: U)`). When you say so (**U**, or `start.cmd update
     --program [--now]`), it downloads `project-reclaimer-dedicated-v<version>.exe`,
     checks it against the release's `SHA256SUMS.txt`, puts it in place of those
     servers' copies and the root's (the old ones go to `records/previous-program/`),
     never over a copy that is that version or newer already (a pre-release
     someone put in is left alone: its settings may need it), and restarts each
     server whose copy it replaced **after its game** (players read `PARDON OUR DUST!
     This server restarts after this game: updating to <version>`), or now.
     `[server_updates] install = "after_game"` does it by itself; `"now"` restarts
     at once. U also reaches any self-updating server still behind, and asks the
     program copy of every server with auto_update off for its `--version`, so a
     stopped server's old program is offered too. A copy whose answer can't be read
     is never replaced, and one that doesn't answer isn't counted as behind
     (decision).
   - **How a copy is replaced.** The new program is written beside the old as
     `<file>.new`, then renamed into place, so a crash never leaves half a
     program; when several copies are replaced and one can't be, the ones already
     replaced are put back. The download is checked against its size as well as
     its SHA-256. A release GitHub withdraws is dropped (its download, its planned
     restarts) and never installed. When Huragok's own install for a stopped
     server (the step-in above, or a program older than its file) can't replace
     the file (an antivirus or Controlled Folder Access holding it: the log says
     so), the server starts on its old program and Huragok tries again after 1
     hour, 4 hours and 12 hours, each try one more restart after a game, then
     stops and waits for you: the server's line says `! Project Reclaimer 0.9.21
     didn't install (4 tries): U to try again` (decision).
   - `auto_update` is the only switch. 0.2.1's `[process] disable_auto_update` does
     nothing any more; `check` and the dashboard name it while it's still in
     `settings.toml`.
3. **Workshop mods**: every item's installed version and Steam's newest, and the
   new versions that are waiting, installed after the game or now (see below).
   Installing one here also tries again a version Huragok had stopped trying. Servers
   that share a content folder are refused here: the server program installs the
   new version for all of them once nobody's on.
4. **Clean up**: what updates and mods leave behind, each with its size, numbered to
   pick from, in this order: program files an update set aside
   (`project-reclaimer.exe.old`) and content folders set aside when a server got its
   own, in each folder; the older sets in `records/previous-program/` (the newest
   stays); older Huragok versions in `records/previous-*` (the newest by version
   number stays, 0.2.10 after 0.2.9, and so does the one the last update made); a
   mod's old copy a Workshop update left (only when the live copy is whole: the old
   one may otherwise be the only one left) or a partial new copy it set aside; and
   SteamCMD's copies of mods no server lists any more. Chat logs and settings are
   never offered. A file still in use is kept and says why.

## Workshop mods: new versions after the game
The server program keeps the Workshop items it lists (`[workshop] items`) up to
date by itself, but it installs a new version only once nobody is on a server that
uses the mod. A busy server may never get there, and meanwhile players who already
have the new version can't join it. With `[workshop_updates] install =
"after_game"` (the default) the Superintendent does it at the end of a game:

1. Every `check_minutes` (5) it asks Steam's public Workshop details for each item
   and compares them with the version each server installed. The server saying it
   downloaded a new version counts too. The server's dashboard line shows `mod
   update waiting: <mod> (<date>); installs after this game`.
2. It plans a restart after the game. Players read `PARDON OUR DUST! This server
   restarts after this game: installing a new version of <mod>`, and get the short
   after-game warning once it ends.
3. With the server stopped (the dashboard says PREPARING), the old copy is moved
   aside and `dedicated mods` fetches the new version: about a minute, much less
   when the server already downloaded it during the game. Then the server starts.
   If the fetch fails (an expired SteamCMD sign-in, Steam down), or brings back the
   version it had, the old copy goes back, the server starts on it, and the new
   version is tried again after a later game: at most 3 tries per version, each one
   more restart after a game (decision). Then the log says `… didn't install after
   3 tries …; Huragok stops trying that version until a newer one comes out. To try
   again: U -> Workshop mods`, and the server's line says so too.

PREPARING always ends in a start: each job before a start (this one, or a new
program) has 20 minutes, then the server starts anyway; **X** while it's PREPARING
calls the jobs off and leaves it stopped (what they hadn't finished waits for the
next start). A restart planned after a game is dropped if the server restarts
anyway first (the daily restart, a crash, a stop), so it never restarts twice; its
job still runs at the next start.
A job cut short by Huragok closing or crashing mid-fetch never leaves a server
without its mod: the old copy goes back at once, or as Huragok next starts.

Each server with Workshop items also gets `update_check_minutes = 5` in its
`[workshop]` table (from its next restart), so it downloads new versions during the
game. `"when_empty"` leaves it to the server's own way, `"off"` to you; one server
can differ with `workshop_updates = "..."` in its `[[server]]` block in
`settings.toml`. Servers that share one content folder are always left to the
server's own way: it swaps the mod for all of them at once.

When SteamCMD's sign-in has expired, the server's line says so in red: press **I**
to sign in again. A server whose SteamCMD can't fetch anything here (one copied in
from another PC names a SteamCMD that isn't on this one, or an account with no saved
sign-in here) isn't restarted for a new version at all: the update waits, its line
says `it can't be fetched: … press I`, and players aren't told anything. Once I has
set SteamCMD up, the next look at Steam plans it as usual.

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
  `admin`, `reload` and setting the join `password` need owner, while `admin` or
  `password` typed alone only ask), and the reply says so otherwise. A command in
  `[off]` passes for nobody, owners included;
- one command per message, on one line: a line with a new line, a tab or another
  control character in it is refused, and so is a blank word in the protocol's
  `args` (a command sent with words is never taken for a question: before, an
  empty word let a moderator clear the owner-only join password);
- the server reads **who acted**: players see "kicked by Alice", the server log
  and Huragok's log record it;
- events reach every signed-in moderator; players' addresses and device
  fingerprints only reach admins, in events and replies alike, on both protocols
  (decision: below admin, the relay's `status` hides the server's own address too,
  and IP addresses typed in chat are hidden as well);
- **one** upstream spot serves everyone, and a wrong password locks nobody out
  of the server (five in ten minutes lock that address out of the relay);
- the dashboard header shows who's signed in, on Vergil's line (`VRGL … consoles:
  Alice (LG Test Server)`).

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

## What a server plays next
The dashboard's GAME column shows the game under way and, when one is set, the
next one (`playing Guardian Slayer → next Valhalla Slayer`, from a `nextmap`
override), or `(voting)` while a playlist ballot is open. A stopped server shows
what it's set to play (`plays playlist infection`).
