# Changelog

## 0.4.3
**D is now where every setting is changed, a step at a time: Huragok's own files as
well as each server's, with no file format to know.** D lists Huragok's files above
the servers: **S** how Huragok runs (`settings.toml`), **C** roles and commands
(`commands.toml`), **A** Vergil's word list (`automod.txt`), **N** banned names
(`names.txt`). **E** is gone: **O** (Open) opens the folder first, then every file E
listed, in your editor. Install it with U; your files stay as they are.

- **S, how Huragok runs**: every `settings.toml` setting in thirteen groups (which
  servers and who starts them, how they're run, restarts, the watchdog, the keepalive,
  the event log, automod, the moderators' console, Huragok's updates, the server
  program's, Workshop updates, new servers and SteamCMD, folders and files), each with
  its value, whether it's the default, and what it does. On/off and choices are picked,
  lists of roles by number; `-` puts the default back. **A change that would stop
  Huragok starting is never written**: it's tried on a copy of the file first, read
  the way Huragok reads it as it starts (`check`'s checks too), and refused with why.
  The relay's moderators are here too: add one (name, password, role), change a role
  or password, take one out. Changes apply when Huragok starts again (close the window
  with Q; the servers keep running).
- **C, roles and commands**: owners, moderators and members, added from the players
  Huragok has seen lately (numbered: no player ID to type, though you can) or taken
  out, one role each as `!promote` keeps it; every in-game command with the lowest
  role that may use it, moved to another role or switched off and on; the prefix and
  the cooldown.
- **A, Vergil's word list**: each rule shown as players meet it ("1st: removed for 1
  hour; 2nd: muted for 1 day…"). Add one: its words, then what each strike does from
  four ready-made ladders or your own (a question for the kicks, the mutes and the
  ban), then its name; it's shown before it's added, and a rule the list can't take is
  refused with why. Change a rule's words, strikes or name, take it out, or try a line
  of chat to see what catches it.
- **N, banned names**: add one with a note, take one off, or try a name.
- **A server's page** gains its playlists (P), custom games (G), bans (B) and **its
  mods switched on or off** (M; `content/disabled-mods.json`, applied at its next
  restart, which D offers). The settings in groups are as before; from the D list,
  Esc goes back to the list, and from there to the dashboard.
- **O, Open**: the folder first, then `settings.toml`, `commands.toml`, `automod.txt`,
  `names.txt` and each server's files (its folder first) in your editor, for what D
  doesn't change.

## 0.4.2
A new version of Huragok no longer gets lost among the events: the dashboard's
HURAGOK line says `Huragok <version> is out (U)` until it's installed, in the second
window too. And Huragok can install its own new versions: `[updates] auto_update =
true` installs one as soon as it's found (off unless you turn it on). It replaces
`install_on_server_restart`, which waited for a server to restart. Install it with U.

- **The HURAGOK line** says when a new version is out, when it's installing, and when
  one didn't install (`U tries again`). Before, the only sign was a notice in the
  events, which a server failing its starts pushed off the screen within seconds.
- **The notice says U.** It said "type update here and press Enter", which is how it
  works without the window.
- **`[updates] auto_update = true`** installs a new version as soon as Huragok finds it
  (at its start and every `check_hours`), whatever the servers are doing; they keep
  running while it updates. A version that failed to install, or that didn't start and
  was put back, isn't tried again by itself, only a newer one. `false`, the default,
  waits for U.
- **`install_on_server_restart` is now `auto_update`.** It waited for a server to restart
  or come online, which a server that keeps failing to start never does, so the new
  version that might have fixed it never came. Your `settings.toml` is brought up to
  date at the next start: `true` becomes `auto_update = true` (the old line stays as a
  comment saying so), `false` becomes `auto_update = false`. Until then the old name is
  still read.

**Fixed: a role change that went unseen.** Huragok noticed `commands.toml` changing by
its file time, and Windows moves file times in steps of about 16 ms: two changes in one
step (two `!promote` at once, say) left the second unseen, and the roles in force stayed
the old ones until the file changed again. Now a change Huragok makes itself is read at
once, and any other is noticed by what the file holds, not only its time.

## 0.4.1
Huragok now keeps your files up to date with itself, and puts right what a server
arrives without. A server folder copied in from another Huragok folder or PC gets
its ban list, content folder and Halo 3's files by itself, and waits (WAITING, with
why) instead of failing its starts until it's FAULTED. **I** is SteamCMD now: the
Workshop account as before, plus Halo 3's files. Install it over 0.4.0 as usual
(U, or `start.cmd update`): nothing to reinstall, nothing to start over.

**Your files keep up with Huragok.** An update replaces only the program, so until
now a new setting never reached an existing `settings.toml`. From this version on,
Huragok brings your own files up to date as it starts after an update:

- `settings.toml` gets what's new (this time `[setup] download_game_files`, with
  its comment, and the `[setup] steamcmd` comment that now names Halo 3's files).
  Settings that do nothing any more become comments that say why
  (`[process] disable_auto_update`, retired in 0.4.0), and the repository's old name
  is corrected. Your own values and comments stay exactly as you wrote them: a
  comment you edited, or a setting you wrote as a comment, is left alone.
- `commands.toml` lists in-game commands added since it was written, under their
  default role (a command in no list had that role already: nobody's rights change).
  `PACKAGES/` gets starter packages added since; one you removed stays removed.
- Each file is copied to `records/previous-files/<version>/` before its first change,
  and `records/files.json` says what's been done, so nothing runs twice. `check` lists
  what the next start will bring up to date. A file that can't be read is never
  touched: Huragok says so, as before, and brings it up to date once it's fixed.
- Every release from here on reads your files as any version from 0.4.0 on wrote
  them, and updates them itself.

**A server folder that arrives without what it shared.** A server Huragok makes
shares this folder's `game/` and `bans.json` (`game = "../../game"` in its
`dedicated.toml`). Copied into another Huragok folder without them, it wouldn't start
("missing files copied from your own MCC installation"); worse, the server program
reads a missing ban list as an empty one, so once it did start it would have had no
bans and nobody would have known. Now, as soon as Huragok finds a server folder, and
again before each start:

- **A ban list that isn't there** is made, empty, where the file says (or, when even
  its folder isn't on this PC, the server uses this folder's shared `bans.json`). The
  log says so, and that the old bans stayed where the folder came from.
- **A content folder that isn't there** is made.
- **A game folder without Halo 3's files**: the server is pointed at one here that has
  them. With none, Huragok gets them into this folder's `game/`: copied from MCC when
  it's on this PC (hard links on the same drive: no space used), otherwise downloaded
  with SteamCMD (Halo 3's multiplayer is its own part of MCC on Steam: about 9.3 GB
  down, 5.8 GB kept, since sound is left out). Meanwhile the server shows **WAITING**
  and why, and its failed starts aren't counted. With no way to get them, it waits and
  says to press I. The campaign missions and ODST's files, for a server that plays
  them, come the same way. `[setup] download_game_files = false`: nothing is
  downloaded without you.
- **A start that fails for missing files** (a copy that left out a map) gets them
  before it tries again.
- **A `[workshop] steamcmd` from another PC** is replaced by this folder's SteamCMD.
- **A copy whose original still runs** on the same port waits ("in use by something
  that isn't this server") instead of being watched as if it were this server.
- Only with `backend = "process"`: in a container, or wherever a command runs the
  server, its `dedicated.toml`'s paths aren't this PC's, so they're left alone.

**I is SteamCMD.** It shows SteamCMD and the account, and every game folder: its
Halo 3 maps, the campaign files, ODST, and which servers use it. **1** is SteamCMD
and the account, as before. **2** gets Halo 3's files, **3** the campaign missions,
**4** ODST's files. Each lists every way there is first (copy from MCC here,
download with SteamCMD), with sizes. A download runs in the background while the
window stays free; the servers waiting for the files start once they're in. Without
the window: `start.cmd steam files [halo3|campaign|odst] [--download]`.

- **After an MCC update**, when C's check says to copy `halo3.dll` again and MCC isn't
  on this PC, it offers to download the new files with SteamCMD and swap the engine
  files in (the old ones renamed, as a running server holds them).

**Workshop updates wait for a SteamCMD that can fetch them.** A new version of a
server's mod, when that server's SteamCMD isn't on this PC or its account has no saved
sign-in here, no longer restarts the server: the update waits, flagged "press I", and
players aren't told anything. Before, the server was restarted after each of up to
three games, and each time the old copy went back.

**MCC is found only where Steam says it is**: the registry's Steam folder (Steam's own
links on Linux), the libraries `libraryfolders.vdf` lists with the games each holds,
and the folder MCC's own `appmanifest_976730.acf` names. No drive is searched and no
folder guessed. MCC that Steam doesn't list: name it as `[setup] mcc_folder`.

## 0.4.0
The first release as **Huragok**, for Windows (Linux comes with 0.5.0), with
everything since Reclaimer Supervisor 0.2.1: a setting for every watchdog case,
servers that update themselves, Vergil's mutes and banned names, roles moved a
step at a time in game, Halo 3: ODST servers, and about 180 fixes from an audit of
the whole program before 1.0. A 0.2.1 install can't update itself across the
rename: reinstall it (the README's "Coming from Reclaimer Supervisor 0.2.1").

It was built as 0.2.2 and 0.3.0, neither released on its own; both are in here.

**Reclaimer Supervisor is now Huragok.** It's named for Halo 3: ODST's Engineer,
which mended New Mombasa's dying city AI and carried what was left of it out of
the city. The two minds it carried are its parts now: **the Superintendent** keeps
the servers running (starts, restarts, countdowns, the watchdog), **Vergil**
watches over the players (automod, in-game commands, bans, the moderators' relay),
and **Huragok** itself builds and mends (new servers, content, the Workshop,
updates).

- **The window:** the Superintendent's face on the second line shows how the
  servers are doing, with one of the notices it posted on New Mombasa's signs:
  `[^_^] KEEP IT CLEAN!` with servers up and players on, `[o_O] PLEASE REMAIN
  CALM!` during a countdown, `[>_<] METROPOLITAN EMERGENCY!` when one is FAULTED.
  The keys are grouped by the part that answers them, every screen's title names
  its part (`SUPERINTENDENT › RESTART`), and every event line says who spoke:
  SUPT, VRGL or HRGK, the way the Rookie's HUD labelled the Engineer "VRGL". H
  explains who's who and what the colours mean (the VISR's: green friendly, red
  hostile, yellow look here). `status` shows the face too.
- **What players read:** countdowns start `PLEASE REMAIN CALM!`, the restart itself
  and a restart after the game `PARDON OUR DUST!`, a cancelled countdown ends
  `Keep it clean!`, and automod's messages start `KEEP IT CLEAN!`. A new
  settings.toml has `name = "Superintendent"` (was "Supervisor") and `[automod]
  name = "Vergil"` (was "AutoMod"): players read "removed by Vergil". An existing
  settings.toml keeps what it says.
- **The watchdog's actnow** cases log `METROPOLITAN EMERGENCY! Restarting it now: …`.
- **New names:** the repository is
  [Project-Reclaimer-Huragok](https://github.com/LittleGroove/Project-Reclaimer-Huragok)
  (the old address forwards), the release zip `huragok-<version>.zip`, the command
  `huragok`, the variables `HURAGOK_CONFIG` and `HURAGOK_LAUNCHER`, the start-at-boot
  item `Huragok.cmd` / `huragok.service`, and by default `logs/huragok.log`,
  `records/huragok.sqlite3` and `records/huragok.pid`, and the Docker image's volume
  `/huragok` (was `/supervisor`: a compose file that still mounts `/supervisor`
  starts with fresh files). Settings keeps every key and section name it had
  (`[watchdog]`, `[automod]`…); the settings.toml template groups its sections under
  the part they belong to.
- **Coming from Reclaimer Supervisor 0.2.1: install this version by hand.** 0.2.1
  can't update itself across the rename; the README says how (close the window,
  replace `engine` and the start scripts, start it). Your settings, servers and
  records stay, and 0.2.1's lock (`records/supervisor.pid`) still counts, so the two
  never run at once.

**A setting for every watchdog case.** Each case has its own line in
`[watchdog]`: `"actnow"` restarts right away, `"act"` at the end of the game (right
away when no game is on or nobody is playing), `"watch"` only logs it and shows it;
each has its numbers next to it in settings.toml.

- **`dead`** (actnow): every player dropped at once. **`no_joins`** (actnow, new):
  players keep connecting and none gets in while there's room: a dead server
  even when it doesn't say "Host roster lost". On the live server's log it fired
  only while the server was dead, including the night one sat dead for six hours
  on a version without that line. **`stuck`** (actnow): on the scoreboard after a
  game with players waiting, now after 3 minutes instead of 10 (`stuck_minutes`;
  over 51 healthy game ends on the live server, the next game was loaded and
  running within 109 s). **`slow`** and
  **`very_slow`** (watch): the frame rules. **`memory`** (act, new): the server
  program using more than `memory_mb` (0, off, until you set it).
- Replayed against the live server's log with the real code and these defaults:
  every restart fell inside a stretch where the server was dead; the mid-game drop
  that recovered, the engine crashes and the frame warnings restarted nothing.
- A case that comes up while another is already there still acts (a server shown
  as very slow all game is still restarted when nobody can get in); an actnow case
  turns a restart planned for after the game into one now. A restart now also
  replaces any restart planned for later, which used to come back after the next
  game.
- `check` names a watchdog setting that isn't one, or a case set to something
  other than watch, act or actnow. 0.2.1's `slow_frames` still counts when `slow`
  and `very_slow` aren't set.

**A frozen server comes back in about three minutes.** The live server stopped
twice in a row: nothing in its log for 76 minutes with a player on, then again 2 s
into the next start, for 10 minutes. The cause was reproduced here: text selected
in the server program's own console window holds its log, and after about 75 KB of
output blocks it. Servers Huragok starts have no console window, so that can't
happen to them. And with a test server's game frozen outright, Huragok's keepalive
restarted it 3 minutes 4 seconds later; it was answering again 8 seconds after
that.

- **The players already on are known again.** Since 0.2.0 the 30-second look at
  each server stopped after its first try, so players who were on before Huragok
  connected went unlisted, a countdown could be skipped as if nobody was on, and
  the keepalive judged a hang on `status.json` alone. It now keeps going, and
  says so in the log if a reply ever can't be read.
- **Every run gets its own three minutes.** The keepalive judges a server from
  when this run came up, so one that freezes as it starts is restarted three
  minutes later, not up to ten. It asks once per run; the hourly restart budget
  still stops a loop.
- A server started some other way that hangs is reported ("can't restart it: …")
  instead of an error about an unawaited task.

**A server can update itself.** `auto_update = true` in a server's dedicated.toml
lets Project Reclaimer install its own releases: it looks when it starts and
hourly, checks each download against the release's SHA-256 list, and restarts its
game on the new version (tested: back in 3 seconds; the process Huragok holds stays
up through it). Huragok used to switch this off for every server it started; now the
file decides, and it is the only switch.

- **D shows and switches it**, and offers to restart the server so it takes
  effect: after this game, when nobody's on, now, or later.
- Huragok leaves these servers to it: U and `update --program` install only for the
  servers with auto_update off. The log says when a server updated itself and the
  version it's on now. **`[server_updates] follow`** (on): once one server has
  updated itself, the other self-updating servers restart after their game; they
  look for releases as they start, so they are on the new version within a game
  instead of within the hour.
- **Huragok makes sure it happened** (`[server_updates] fallback`, on). It looks at
  GitHub hourly (`check_hours = 1`, was 6), and reads what each server's console
  says about its own updater. A server that says its update failed, or is still on
  an older version 70 minutes after the release came out, gets the release from
  Huragok instead: downloaded and checked once, put in place of that server's
  program while it's stopped after its game. If it updates itself first, Huragok's
  restart is called off. U and `update --program` reach self-updating servers still
  behind too.
- **A program older than its settings is replaced.** The program reads
  `dedicated.toml` before it looks for releases, so one older than the file stops
  at a setting it doesn't know and can't update itself (0.9.15 can't read a file
  0.9.20 wrote). When a start fails on an unknown setting, Huragok compares the
  program's `--version` with the newest release: older, and the newest is put in
  place before the next try (servers with auto_update on); the newest already, and
  the log says that line is a mistake.
- **Never back to an older program.** U, `update --program` and
  `[server_updates] install` ask each copy its `--version` and leave one that is
  the release or newer (a pre-release, say) as it is, restarting only the servers
  whose copy was replaced. A server whose settings need its newer program would
  otherwise stop starting.
- After a self-update, the `dedicated` Huragok holds still runs from
  `project-reclaimer.exe.old`; once nobody is on, Huragok restarts it on the new file
  and removes the old one, so the next update has nothing in its way.
- **New servers update themselves** (`[setup] auto_update = true`; `new
  --no-auto-update` for one that doesn't), with a copy of the program of their own.
  `check` calls it a PROBLEM when a self-updating server shares its program file with
  another server, or can't write to its folder.
- `[process] disable_auto_update` is gone: in a `settings.toml` from before, it
  silently switched every server's own updater off. `check` (under "Old settings")
  and the dashboard now name it until the line is deleted, and Huragok no longer
  passes `RECLAIMER_DEDICATED_AUTO_UPDATE` to the servers at all.

**Workshop mods get their new versions after the game.** The server program
updates its Workshop items only once nobody is on a server using the mod, which a
busy server may never reach, and players who already have the new version can't
join it meanwhile. Now the Superintendent looks at Steam every 5 minutes
(`[workshop_updates] check_minutes`) and, when a mod has a new version, restarts the
server after its game: the old copy is moved aside, `dedicated mods` fetches the new
one (tested on a real update: 38 seconds for a 2 GB map), and the server starts
again. If the fetch fails, the old copy goes back and the server starts on it.

- Players read why: `PARDON OUR DUST! This server restarts after this game:
  installing a new version of <mod>`. The dashboard shows "mod update waiting" on
  the server's line, and PREPARING while the mod is swapped in.
- `[workshop_updates] install`: `"after_game"` (the default), `"when_empty"` (the
  server's own way), `"off"`; one server can differ with `workshop_updates` in its
  `[[server]]` block. Servers that share a content folder are left to the server's
  own way, which swaps a mod for all of them at once.
- Each server with Workshop items gets `update_check_minutes = 5` in its
  `[workshop]` table, so it downloads the new version during the game and the swap
  is quick. A server that says it downloaded one counts as well.
- **U → Workshop mods** lists every item's installed version and Steam's newest,
  and installs waiting ones after the game or now.
- An expired SteamCMD sign-in is shown in red on the server's line with "press I
  to sign in again"; until then mods can't be fetched or updated.

**A status card per server (V).** One screen with everything the Superintendent
keeps an eye on: running (uptime, RCON, status, process, memory), joining (address,
listed or hidden, password, players, ping limit, welcome), what it plays, its program
version and how it updates, its Workshop items (installed and Steam's versions, a new
one waiting, the SteamCMD account, disk use), health (the watchdog and why, the starts,
restarts and crashes of the last 24 hours with their reasons), safety, settings, the
schedule, and problems. From the card: its settings, game content, restart, console,
updates, clean up, with the server already picked. The dashboard's line under a server
now says why the watchdog is worried, how many restarts and crashes it had in 24
hours, and in red a settings reload that failed, until one goes through. Huragok
keeps each server's starts, restarts, stops and crashes for a week in its record.

**Everything about a server on D.** The settings come in groups (Joining, What it
plays, Gameplay, Voice and chat, Safety and admins, Mods and the Workshop, Updates,
Listing and network, Remote console, Folders and files, Huragok's own), each listed
with how many it holds and its values before you pick. New on D: **what it plays**
(a playlist, one map and game type, a saved custom game, or campaign co-op with the
mission by name, rally point, difficulty, custom characters and modifiers; switching
takes the other kind out), the VPN lists, this server's own admins, console port and
password, the master's peers and origin, Workshop items and SteamCMD, the game,
content, data and ban list folders (checked to be there), and Huragok's own
autostart, daily restart and Workshop update mode. Every choice is a numbered list;
every unset value shows the program's default; passwords show only as "set".
**Rename** a stopped server (its block, settings.toml, data folder, records, console
log and folder all follow), or give one that shares a content folder **its own**.

**Game options without numbers.** The full catalogue of game options (every key the
program accepts, its label, range and the meaning of every number, generated from
the program's own answers) drives two editors laid out like the game's Game Options
screen:
- **One playlist game** (G → Playlists → Edit → Change one game): the name players
  see, weight, the game type's rules and options, traits by who gets them, team
  characters, sprint, a vote image (from `content/Images`, or import one), base map,
  anti-cheat off for that game. Choices are written by their label, so the playlist
  stays readable.
- **Custom games** (G → Custom games): a game type with rules of its own, saved as a
  `.json` in `content/Game Modes`: new from a game type, copy (the shipped scripted
  modes too), edit, show what it changes, play it on the server, delete. The server
  program checks each before it's saved. `variant list | show | check` from a terminal.
- **Playlists from a mod's maps** (G → Playlists → M), named the way the server finds
  the right mod (`workshop:<item>/<map>`).

**Upkeep jobs:**
- **U → Clean up**: what updates and mods leave behind, with sizes: old program
  copies, the old copy of a mod a Workshop update replaced, SteamCMD's copies of mods
  no server lists any more, folders set aside, older Huragok versions. Never chat
  logs or settings.
- **C** offers to open the firewall ports the check prints (in an administrator
  PowerShell, each rule named for its ports and added only once) and, after an MCC
  update, to copy Halo 3's engine file from MCC.
- **New servers keep their own content folder** (`[setup] share = ["game", "bans"]`):
  Workshop updates after their own games, custom games their own. Packages keep the
  folder's `[defaults]` (not the join password or admins).

**The settings screen (D), fixes:**
- **Fixed: changing `admins` broke dedicated.toml.** The file `dedicated init`
  writes has the list over three lines; only the first was replaced, leaving a
  stray `]`, and the server couldn't start again. Lists and other values over
  several lines are replaced whole now, and a `#` inside a quoted value (a
  password) is no longer taken for a comment.
- A welcome line keeps its commas: `|` separates lines.
- A value a server sets itself can be taken out (`-`, or D on on/off settings), so
  `[defaults]` or the program's own default applies again.
- New on D: `auto_update`, `[workshop] update_check_minutes`, `trust_player_games`.
  `admins` must be player IDs. `load_timeout` applies at once (it said "when
  empty").
- Adding `[workshop]` to a file brings the commented example `dedicated init`
  wrote to life instead of adding a second table below it.
- New servers get the SteamCMD account the I key set up, so their Workshop items
  can be fetched from the start.

**Vergil mutes.** A line of `automod.txt` now has five parts,
`words | kicks | mutes | ban | name`, and each strike is the next step down it:
the kicks, then the mutes, then the ban.

```text
rudeword    |    | 0, 1d, 3d, 7d  |     | rude       muted 1 day, then 3 days, then a week each time
worstword   |    |                | 0   | threats    banned the first time
badword     | 1h | 1, 1d, 7d      | 3   | slurs      out 1 hour, muted 1 day, muted 7 days, banned
```

- **mutes** is how many strikes come before the mutes (`0`: the first offence),
  then how long each lasts; the last repeats, and `perm` mutes for good. **ban** is
  how many strikes come before the ban. A line in the old four parts
  (`words | timers | chances | name`) reads as it always did, and `check` points it
  out; a three-part line with chances is now a mistake the log explains.
- **Mutes last.** The server forgets its mutes when it restarts, so Vergil keeps
  its own: a muted player who joins any server is muted again for what's left
  (`KEEP IT CLEAN! You're still muted (rude): 2 days 23 hours left.`). A strike on
  someone who's off mutes them as they join. `!unmute` from anyone ends it for
  good; `!pardonplayer` unmutes. `!strikes` shows how long a mute has left.
- **Names are kicked, not struck.** A name with a listed word in it is kicked as it
  joins, every time, until the player changes it: no strike, no ban, and Vergil
  lets them straight back in so they can return with another name (before, a name
  earned strikes and bans like chat). **`names.txt`** is the new list of banned
  names, written on the first start; `!strike <player> name` adds a player's name
  to it and kicks them, `!unbanname <name>` (admins) takes one off, and edits apply
  within seconds, kicking anyone already on with a name you add. `[automod] names`
  names the file; `check_names` now covers only the word list's words in names.
- **Members, and no vote kicks for anyone you list.** `commands.toml` has a new
  role, `[member]`, between everyone and moderator: players you list by ID, with
  no commands of their own yet. No vote can kick a member, moderator, admin or
  owner now: Vergil keeps each server's vote-kick immune list (the console's
  `immune`) in step with `commands.toml`, so the server refuses a vote to kick
  them as it's called, and Vergil tells the caller why (`BE A HERO! REPORT
  VIOLATORS! Charlie is a member here, so no vote can kick them.`). On a server
  program without the list, a vote on one of them is cancelled at once. An
  existing `commands.toml` has no `[member]` section: add one (`check` points it
  out).
- **One role each, moved a step at a time, in game.** `!promote <player>` moves
  someone one step up the ladder (player, member, moderator, admin), up to the role
  below yours: moderators make members, admins make moderators, owners make admins.
  `!demote <player>` moves someone below you one step down (an admin becomes a
  moderator, a moderator a member). Nobody is two things: moving someone takes them
  out of their old role. `!roles` lists who has which; `!admin add` (owners) makes an
  admin in one step and `!admin remove` a moderator. Moderators and members go into
  `commands.toml` on a line of their own with who they are and who moved them, the
  rest of the file as you wrote it, in force at once (the vote-kick immune list too);
  a player who's on is told. Owners change only in the file; `check` names anyone
  listed under two roles.
- **A ban takes every role away**, however it's given; only an owner can ban an
  admin (nobody acts on their own role or above).
- **`!banname <#number | name>`** (moderators): ban a player's name, or a name
  itself, straight away; whoever uses it is kicked as they join until they change
  it. `!unbanname` stays the admins'.
- **Dry run** no longer tells a player they were removed when nothing was done.
- New events: `automod.name`, `automod.name_banned`, `automod.name_allowed`,
  `supervisor.commands_reloaded`, `log.vote_refused`; a new message, `muted`, for
  `[automod] messages`.

**Project Reclaimer 0.9.19 and 0.9.20.** Halo 3: ODST servers, the camera, and two
renamed game option labels.

- **Halo 3: ODST servers.** **D → What it plays** has a fifth kind, **Halo 3: ODST**:
  a campaign mission (where it starts, the difficulty, the campaign modifiers) or
  Firefight (one of ten maps and one of ODST's Firefight game types), each picked
  from what `dedicated content odst` lists for the server, never typed. An ODST
  server needs ODST's files in its game folder (about 3.5 GB): D, a new server and
  `check` (C) offer to add them from the MCC on this PC that has ODST installed,
  hard-linked on the same drive, into the root's shared `game/` too. A new starter
  package, *ODST Firefight* (`needs_odst = true` is new in `package.toml`), and
  `content odst` lists ODST's missions, maps and game types. The dashboard and the
  status card say what an ODST server plays ("ODST Firefight Crater (Night), Ff
  Classic (heroic)"), where they used to say "Slayer on Guardian".
- **Its console.** Huragok refuses the commands that would change an ODST server's
  game, teams or rounds (`map`, `mode`, `load`, `nextmap <game>`, `endgame`, …),
  from the window, `rcon`, the relay and Vergil's in-game commands (`!map`, `!load`,
  …), with the program's own words: 0.9.20 still
  takes `map`, `mode` and `load` in an ODST lobby and queues a Halo 3 game next,
  though its guide says it refuses them.
- **The Superintendent and ODST.** A game that goes straight back to a new lobby,
  as an ODST server's does, now counts as over for a restart planned after the game
  (it waited for the last player to leave, or 30 minutes). The watchdog's
  "nobody can join" case counts ODST's own "joined the game in progress" line as a
  join, and the console's join event once that player is still on two minutes
  later, so a healthy ODST server can't look like one that lets nobody in. After an
  MCC update, C copies `halo3odst.dll` again beside `halo3.dll` when the game folder
  has ODST's files.
- **The camera** (0.9.20): `camera` in **D → Gameplay** (first_person, the default,
  which now also puts third-person character mods in first person; player_choice;
  third_person), from the next game; the status card shows it.
- **Campaign sprint** (0.9.19): the campaign modifiers in D (Halo 3 and ODST
  campaigns) ask about sprint, its speed (110-200%) and stamina (1-30 s). Modifiers
  D doesn't ask about, such as `skulls`, and a campaign's `customization` are kept.
- **Renamed labels.** 0.9.19 renamed the character set "Vanilla characters" to "Halo
  3", and a playlist naming the label its program doesn't take stops `dedicated
  check`, so the server won't start (0.9.18 refuses "Halo 3", 0.9.19 on refuse the
  old one). The grenade regeneration "On, with firebombs" is what every version calls
  it; the "On with firebombs" some files have is refused by all of them. The game
  option catalogue is re-probed against 0.9.20 (the same keys and ranges otherwise).
  Huragok reads either label; as it starts a server it writes its playlists' labels
  the way that server's program calls them, the option editor writes the character
  set as its number (which every version takes), and `check` names any label a
  server's program would refuse. `tools/reprobe_gameoptions.py` repeats the probe
  after a release.
- Nothing needed for the rest: Huragok's program updates ignore the new
  customization editor download; nothing in Huragok limited character packs (now
  128 a game); Theater and the ODST controls are the players' side. Everyone needs
  the same version, as since 0.9.16: `auto_update` and **U** keep the servers on it.

**The audit before 1.0.** Every part of Huragok was read through and tried against
the real server programs (0.9.15 to 0.9.20), the live server's logs and test
servers; about 180 faults were fixed, each with a test (over 750 tests, from 393). A few
fixes made a choice you may want to undo: each is marked *Decision*.

**Keeping servers up** (the Superintendent)
- **FAULTED is a pause, not the end.** A server that failed `max_failures` starts in
  a row, or restarted more than `restarts_per_hour` times in an hour, used to wait
  for someone to press S, however long that took. The Superintendent now tries once
  more after 30 minutes, then every hour, until a run lasts `good_run_seconds`
  (then everything is back to normal); a try that fails goes straight back to
  FAULTED with the next wait, so it never thrashes. The server's line says when
  (`FAULTED - tries again in 29m`), as do `logs/faulted-<server>.txt` and the
  status card; S starts it at once, X leaves it stopped. `[restarts]
  faulted_retry_minutes = [30, 60]` sets the waits (the last repeats; `[]` waits for
  a person, as before). New event `server.fault_retry`; `server.faulted` has
  `retry_at`.
- **Huragok starts itself again if it stops unexpectedly, and takes a new version
  back out if it won't start.** The launcher (`start.cmd`, `start.sh`, a task at
  boot) runs Huragok as a process of its own and starts it again after 5, 15, 30
  and 60 s, then every 5 minutes, for as long as it takes (back to 5 s after a run
  of 10 minutes), saying so in the window and the log: `launcher: Huragok stopped
  unexpectedly (exit code 1); starting it again in 5 s.` (in `logs/launcher.log`
  when settings.toml can't be read). A new version that ends within 2 minutes of
  starting is replaced by the one before it, noted in `records/failed-update.json`,
  and not installed again by itself (Huragok reads that note as it starts; a person's
  `update`, or a newer release, still tries). None of this touches the servers;
  `start.cmd stop` or `close` during a wait ends it.
- **Every deliberate end has its own exit code**: 0 done, 1 it didn't work, 2 a
  settings problem, 3 first start, 4 already running (was 1, the same as a crash),
  5 `status` with nothing running, 10 start the new version.
- **A settings problem keeps the window open** to read (a double-clicked window
  flashed the error and closed, and so did a crash at start). With `--no-menu` (a
  task at boot, Docker) it's tried again every 5 minutes, or as soon as settings.toml
  changes; meanwhile the folder is let go, so `start.cmd stop` and `status` act at
  once (`stop` used to wait 10 minutes for an answer nothing could give).
- **Stopped servers no longer slow the others down**: each stopped server's port
  check held up every server's care for half a second a second on Windows; ports
  are now read from the system's table, once a second for all of them.
- **A stop that didn't stop says so** (`! couldn't stop it: …`, the process named in
  the log, no restart reported) instead of "Stopped." with the server still up; the
  process is still ended when Windows' Ctrl+Break helper fails. `start.cmd stop`
  names what it couldn't stop. *Decision*: such a server stays running and marked
  stopped.
- **A restart planned after the game ends with the run it was for**: one dropped by
  a restart that came first (the daily restart, a crash, a stop) no longer restarts
  the server a second time; its job still runs at the next start. A new run's first
  lobby no longer counts as the old game ending.
- **PREPARING always ends in a start**: each job before a start (a program swap, a
  Workshop install) has 20 minutes, then the server starts anyway; jobs added
  meanwhile wait for the next start; X during PREPARING calls them off.
- **A new Huragok waits for restarts in progress**: an installed update (or a manual
  `update`) relaunches once no server is part way through a restart, a start, a
  countdown or a job. *Decision*: it waits up to 30 minutes, then goes ahead.
- **A part switched off for failing comes back** after 10 minutes (logged,
  `supervisor.part_enabled`); a slow or restarting server's console error no longer
  counts as the part failing.
- **The servers outlive a Task Scheduler task** (Windows): they leave Huragok's job
  object where Windows allows it.
- **`restart` with no server named** restarts the servers that are up ("not running,
  so not restarted"); naming a stopped one still starts it.
- Timers don't jump with the clock: countdowns, backoff and plan ages run on a
  steady clock, and a daily restart inside the hour the clocks skip still happens.
- docker and command backends: a server that stopped answering is revived with the
  restart command (a "start" did nothing to a running container); a restart-only
  command honours a stop asked meanwhile and counts toward the hourly budget.
- `records/running-servers.json` is written in one step and keeps servers left out
  of this run; a server folder found while running is adopted if its server runs.
- A request file of the wrong shape is answered "not understood" and can't stop the
  fleet.
- The keepalive also covers a first run that froze before writing status.json.
- Start at boot (W) works from a folder with accents or `%` in its name; the Linux
  unit's command is quoted.

**The watchdog and the console** (the Superintendent)
- **`no_joins` works while Huragok's console is connected.** The console's join event
  comes as a player is let in, before the map loads, and a dead server keeps letting
  players in and dropping them, so `no_joins` could never fire. A console join now
  counts once that player is still on 2 minutes after connecting; the log's roster
  line and ODST's "joined the game in progress" count at once. Replayed over the
  live server's logs (12,000 connections): every dead stretch restarted, nothing new
  on the healthy ones.
- **Typed text can't fake a dead server.** A `say Host roster lost 99 of 99 players…`,
  a kick reason, or a banned player's ban reason (echoed at each refused join)
  restarted the server with no warning; seven in an hour FAULTED it. Patterns now
  match only at the start of the server's own lines, and echoes are recognised first.
- **`stuck`, and `no_joins`' "is there room", are judged only while the console is
  connected**: after a console drop, a frozen phase restarted a server mid-game.
- **A server that died during a countdown someone then called off is restarted**: a
  case that came up while the server was busy (a countdown, a start) gets another look
  every minute while it lasts, where it used to wait until it cleared.
- **Names in the server's log stay names**: a player called "007" was the number 7,
  so a vote refused for them never reached Vergil.
- `very_slow`'s reason says "falling behind", as the server does ("under 60 fps" was
  Huragok's own; the server's lines say under 54 frames a second).
- **A refused console password no longer ends Huragok's console for good.** It tries
  again when the server's `instance.json` names another password (looked at every
  5 s; the server writes it as it starts, so a new password in dedicated.toml counts
  from its next start), once after the server restarts (at most twice in 10 minutes, so it can never
  lock the machine out itself), and every 10 minutes. A lockout another tool on the
  machine caused is reported as one (new event `log.rcon_wrong_password`;
  `rcon.auth_failed` has `lockout`), not as a wrong password.
- The console connects to IPv6 addresses (`[::1]`), never goes through a system
  proxy, and survives an unexpected error with its usual backoff.
- The 30-second look at the players no longer undoes joins, leaves and phase
  changes that arrived meanwhile.
- A new server's first log lines are no longer lost; a line cut by the server's log
  rollover is finished or dropped, never glued to the next; on Windows the server
  can always rename its log while Huragok reads it.

**Vergil**
- **An invisible word in automod.txt is refused.** A zero-width space, a lone accent
  or a Hangul filler on a line matched every chat line: everyone banned. The line is
  refused, with the character's code.
- **automod.txt and names.txt saved by any Windows editor are read**: UTF-8 (with or
  without BOM) or Notepad's "Unicode"; an ANSI file is refused with "save it as
  UTF-8". A file an editor is still saving keeps the old rules (automod used to
  switch itself off in 25 s).
- **Numbers aren't words**: "respawn in 45s", "455 kills" and "Team 455" no longer
  hit a three-letter word; a listed number (1488) still matches as written.
  *Decision*: a disguise with more digits than letters ("a55") isn't caught either.
- More disguises caught: up to 4 of any punctuation between letters, `()` for o,
  invisible and tag characters.
- **Moderators quoting a word aren't struck**: `!strike bravo slurs he said <word>`
  from someone who may use `!strike` isn't chat; a player's `!help <word>` still is.
- **Who a command is for** (`!warn !mute !kick !ban !strike !strikes !pardonplayer
  !admin add`): `#number` or the number alone, a full player ID, an exact name on the
  server, a name Vergil has seen, or 8+ characters of an ID. The start of a name
  alone acts on nobody ("Nobody here is called 'bobb'. Did you mean Bobby (#6)? Use
  #6."): with Bob gone, `!ban bob` used to ban Bobby. Kick, mute or warn of a player
  who left: "Bob isn't on this server now." *Decision*: `!ban <a name Vergil has
  seen>` bans that player's ID while they're away.
- **Rank counts for IDs too**: an admin can't `!ban` an owner's player ID ("is an
  owner; you can't do that to them").
- **`!unmute` reaches a player who left** and ends Vergil's own mute too.
- **Mutes come back** after Huragok's console reconnects, and after a moderator's
  shorter mute runs out over Vergil's longer one.
- **`!pardonplayer` lifts only the bans Vergil gave**: it used to lift an owner's
  permanent cheating ban along with an automod strike. With someone else's ban on
  that player it lifts nothing and says whose ban stays.
- **Five slow replies no longer switch every `!` command off**: a reply a slow or
  restarting server can't take is dropped.
- **commands.toml missing for a moment** (an editor saving it) no longer takes
  everyone's role away, or their place on the vote-kick immune list.
- `[automod] messages` with a stray brace: `check` names it (write `{{` or `}}`); at
  run time the default message is used (automod used to switch itself off).
- An unnamed automod.txt line whose strikes add up is pointed out by `check` and the
  log: name it, as its strikes are kept under `rule2` and move if a line is added
  above it.
- A strike's kick lands on the server the player is on; with them on none, the
  strike counts and the reply says nobody was kicked.
- Seen names are found whatever their case (Élodie, ÉLODIE).
- **The relay**: a moderator could clear the owner-only join password (`password`
  with an empty word passed as a question). A command with words is never a
  question now, and blank words, control characters and second lines are refused;
  `[off]` applies through the relay, owners included; moderators below admin get no
  addresses or device fingerprints, in replies or events, on both protocols.
  *Decision*: the relay's `status` hides the server's own address from them too, and
  IP addresses typed in chat. A blank command is refused, and one over 16 KiB is
  answered "Not sent: …" (it ended the moderator's session); a `password` line is
  masked in the logs whatever space follows the word.

**Updates**
- **A failed Workshop fetch no longer keeps a server stopped.** It looped in
  PREPARING forever (an expired SteamCMD sign-in, Steam down). Now the old copy goes
  back, the server starts on it, and the new version is tried after a later game, at
  most 3 times per version (`… didn't install after 3 tries …; Huragok stops trying
  that version until a newer one comes out. To try again: U -> Workshop mods`); a
  fetch that brings back the same version counts as a failure. *Decision*: each try
  is one more restart after a game.
- A Workshop job cut short (Huragok closed or crashed mid-fetch) puts the old copy
  back, at once or at the next start. U → Workshop mods refuses servers that share a
  content folder (the server program installs those).
- **Huragok's own update is all or nothing**: renames only (a "rollback" could leave
  `engine\huragok` empty), checked before it's called done, and the old files go back
  if the new version's Python packages don't install. The backup is
  `records/previous-<version>/` (one already there is kept as
  `previous-<version>-<date>`), named in `records/last-update.json`. A version that
  failed to install isn't tried again at every server restart. U says when GitHub
  can't be reached (it said "the newest version").
- **Commands run the server's own program copy**: once servers updated themselves,
  every Workshop job failed on the root's stale copy. A command for one server folder
  runs its own copy; the rest use `[setup] program`, or else the newest copy here.
- **The server program is never half-written**: it's written as `<file>.new` and
  renamed in, an install over several copies is all or nothing, and a release GitHub
  withdrew is dropped, never installed. *Decision*: a swap that keeps failing (an
  antivirus, Controlled Folder Access) is tried again after 1, 4 and 12 hours, then
  waits for U (`! Project Reclaimer X didn't install (4 tries): U to try again`).
- Downloads are checked against their size; a cut-short one is never used.
- U asks the program copy of each server with auto_update off for its `--version`,
  so a stopped server's old program is offered. *Decision*: a copy that doesn't
  answer isn't counted as behind (and one whose answer can't be read is never
  replaced).
- A `dedicated` command cancelled with Esc, or timed out, ends its whole process
  tree, SteamCMD included.
- Clean up keeps the newest Huragok backup by version number (0.2.10 after 0.2.9)
  and the last update's, and offers a mod's old copy only when the live one is whole.
- The firewall rules (C) report a declined administrator prompt or a failed rule;
  a path with an apostrophe works.
- After an MCC update, C leaves a game folder that is MCC's own alone, and names
  every server sharing the game folder.

**Setting up and D**
- **D's Enter leaves the value as it was**: it emptied it (the console password, the
  admins, the Workshop items). Emptying a value asks first, and the console password
  can't be emptied there. Passwords show as stars while typed and as `********` in
  window.log; a console line with a password is masked in every log.
- **D refuses what the server program would refuse**: min players above max, more
  than 16 on a campaign or ODST server, a host name over 48 characters, a malformed
  stats server, public address, master origin or console address, a port a server
  here uses (disabled ones too), a rally point past the mission's last; and a
  setting the server's program is too old for ("camera needs Project Reclaimer
  0.9.20 or newer (this server has 0.9.18)"). *Decision*: refused, not written with
  a warning.
- **Every change to a host's file is checked before it's written**: the change is
  made in memory and read back, and the file is replaced only if exactly that
  changed; otherwise it's left as it was and the error says to edit it by hand (E).
  Valid but unusual files (dotted keys, inline tables, `[` lines in a list) used to
  be corrupted, and a failed rename tried again made settings.toml unreadable.
- **Rename** needs the server stopped (not waiting to restart after a crash), and a
  step that fails puts every step back.
- **Playlist labels by version**: 0.9.18 servers' valid playlists were rewritten into
  ones 0.9.18 refuses, at every start; now each server's labels are written the way
  its own program calls them. *Decision*: the option editor writes the character set
  as a number, which every version takes. "On, with firebombs" was never a 0.9.19
  rename: every version calls it that.
- **"Use this playlist"** takes out the server's map, game, variant or campaign in
  the same write (the program refused a server with both).
- A playlist a server plays is checked as a draft before the live file is replaced
  ("Leave it unsaved" otherwise).
- **New servers**: names of 1-48 characters with no invisible characters, not a
  Windows device name (CON, NUL…: *Decision*); ports 1024-65535; players 1-127 (16
  for co-op); disabled and broken servers' ports and names count; a failed build
  leaves nothing behind; autostart off is written before the folder appears; with no
  MCC folder given, `--own` links the root's game folder, never MCC's own.
- **Two names, one file name** ("LG Slayer", "LG-Slayer") would share records, the
  console log and the FAULTED report: the later folder is left out with the reason,
  and `new` and rename refuse such a name.
- Content writes (imports, Forge layouts, playlists, custom games, vote images)
  never change a copy hard-linked into another server.
- Packages: a field of the wrong type marks only that package broken; `map` and
  `game` files are named inside the content folder; applying one never replaces a
  content file already there.
- A malformed ModInfo.json skips that mod; a mod titled ".." can't write outside
  Maps/.
- Steam account names are checked (2-64 letters, digits and underscores); Esc at the
  sign-in writes nothing.
- MCC is found through Steam's own library list too; a path pasted in quotes
  (Explorer's "Copy as path") is taken everywhere one is asked for.

**The window**
- **Ctrl+C asks, like Q**: it ended the servers' care with no question. Ctrl+Z is
  ignored on Linux.
- **Selecting text in the window no longer freezes the servers' care**: the window
  writes from a thread of its own, so a paused console (Edit > Mark) holds up only
  the drawing.
- A frame that can't be drawn shows a red line instead of ending the window.
- "à" and other keys that share a code with the arrow keys type correctly on Windows.
- Names, chat and console lines are cleaned of control characters before they reach
  the terminal or window.log; wide characters (CJK, emoji) line up.
- A second window keeps the last good dashboard (no flashes of "no servers"), waits
  as long as each request takes, and says "no answer yet: it may still be doing it"
  rather than calling it failed.
- Long work (a new server, SteamCMD, the catalogue, the clean-up scan) ignores keys
  pressed meanwhile, and nothing a screen does holds up the servers' care.
- N asks again for a port or a player count out of range; G → Custom games won't
  delete one a server plays.
- *Decision*: the A console still gives the window back after two minutes without
  typing, even while events arrive.

**Settings, commands and the launchers**
- **Settings that do nothing are named**: a misspelt key (`[relay] enable`,
  `[automod] dryrun`) or one under the wrong heading is named at the start, on the
  dashboard and by `check` ("Settings that do nothing"), with what it was likely
  meant to be. **Values an older version wrote are named** ("Values from an older
  version"): `[watchdog] mode = "watch"`, `slow_frames`, `[setup] share` with
  content, `[server_updates] check_hours = 6`, the old repository name. Both are
  warnings: `check` still says ready.
- **Wrong types stop the start with the reason, naming settings.toml** (a `[[server]]`
  `warn_seconds` that isn't a list, `[server]` with one bracket…), instead of leaving
  a server out and blaming its dedicated.toml. Timings have minimums
  (`check_seconds`, `command_timeout`, `handler_timeout`, `stop_timeout` 1;
  *Decision*: `hang_seconds` and `ready_seconds` 30).
- **Windows paths**: the template's examples are in 'single quotes', a backslash
  escape error says how to write the path, a path with a control character in it
  (a `\n` typed in "double quotes") is refused, and `init <folder>` writes paths that
  read back.
- `check` also runs each part's own checks and judges playlists' labels by each
  server's program version.
- Commands wait as long as the work takes (an update 15 minutes, `rcon` 30 s); a late
  answer says "it may still be doing it". A server is named by its name, folder name
  or number (`2`, `#2`), with or without Huragok running. `status` exits with 5 when
  nothing runs and says when Huragok is starting again; `say` to an unknown server
  lists the servers; `-c other.toml` is honoured by the first start.
- **start.cmd / start.sh**: Python in a folder with accents or other letters in its
  path is found (the first start failed with "Check the internet connection"); a
  venv whose Python is gone is made again; packages that don't load are installed
  again, then the error is shown.
- The Docker image runs `start --no-menu`, so a settings problem is retried.
- A `[process] command` starting with `{executable}` is judged on the program it
  names.
- Tests and CI: the tests can't reach past the machine or a real server's ports
  (49170-49199), and no fake server outlives a run, so they're safe on a PC that
  hosts servers; an error nobody sees (an asyncio task's, a destructor's) fails the
  test, and a hang ends the run with every thread's stack. `linux-check` now fails
  when the tests fail; CI runs Python 3.11 to 3.14 and the suite from the release
  zip itself.

## 0.2.1
**The watchdog acts by default.** A server that dies after a game (every player
dropped at once, nobody can join again) or sits stuck on the scoreboard with
players waiting is restarted at once, instead of waiting five minutes for the
server program's own rule. `mode = "watch"` still only logs.

- **Slow frames alone restart nothing** unless `slow_frames = "act"`: they're
  logged and shown in the window. Replayed against the live server's log (27
  starts, 100+ players on a 2 GB map), the frame rules would have restarted it
  mid-game 372 times and faulted it in 22 different hours; a full server is slow
  anyway, and a restart doesn't make it faster. The drop rule fired exactly on the
  six post-game deaths in the same log.
- A server already shown as critical for slow frames is still restarted when it
  then dies or gets stuck.
- Existing installs keep what their `settings.toml` says: one written by 0.2.0 says
  `mode = "watch"`; change it to `"act"`.

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
