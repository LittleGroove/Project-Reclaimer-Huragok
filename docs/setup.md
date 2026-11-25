# Setting servers up

A server's folder is yours: its `dedicated.toml`, its `playlists/`, its content
folder and its game folder. You set a server up by editing those files (or with
the program's own `dedicated` commands); Huragok reads them, keeps the server on,
moderates it, and **warns** about what will probably go wrong, and why. It doesn't
build playlists or custom games, choose what a server plays, or change how it runs.
Anything a server can do by itself (install its own Project Reclaimer releases and
Workshop updates, fetch the mods it lists, switch playlists by lobby size), it
does; Huragok only says so when it doesn't happen.

Huragok writes into a server's files in three cases only:

1. **Moderation**: bans, kicks, mutes, admins and roles through the server's
   console, and the voice toggles (`!toggleglobal`…).
2. **The stats relay**, while `[stats] relay` is on (the default): the server's
   `stats_server` line points at Huragok while it runs, and goes back as it was
   when Huragok stops ([managing.md](managing.md#stats-and-the-relay-switch)).
3. **What you ask for** in the window or on the command line: a quick action, a
   copy, a version put back, a file set aside, a repair. Each is shown before it's
   written, and the version of the file it replaces is kept first, in
   `records/`, so it can be put back.

Huragok's own per-server `intervalmessages.txt` sits in a server's folder too: it's
Huragok's file, not the server's ([managing.md](managing.md#interval-messages-i)).

Setting servers up is the Engineer's (its lines carry the `HRGK` tag). Nothing here
needs a server stopped. Work that takes a while (making a folder, installing
SteamCMD, reading a server's catalogue) says so on the screen and ignores keys
pressed meanwhile, so a stray Esc or Q never answers the next question; the
servers' care goes on throughout.

A write Huragok makes to a `dedicated.toml` when you ask is worked out on a copy
first, and written exactly as shown; a file that changed since it was shown isn't
written over. Settings written in a form the editor won't change (a dotted key such
as `workshop.items = …`, an inline table such as `rcon = { … }`) are refused,
without showing their value: edit those by hand (**O** opens the file).

## A new server: N in the window, or `new`

```text
start.cmd new "LG Test 2"
start.cmd new "LG Test 2" --all
start.cmd new "LG Test 2" --one-server --free-ports --master free --console --yes
```
(Linux: `./start.sh new …`.) **N** asks the server's name, then makes
`SERVERS/<name>/` with the program's own `dedicated init`, and nothing more: no
shared folders, no console password, no `auto_update`, no SteamCMD lines, no
playlist. What `init` writes is what the server gets. The folder is made as
`SERVERS/.new-<its file name>/` (such as `.new-lg-test-2`) and given its name only
once you're done with it, so Huragok never starts a server half made; one that
can't be made leaves nothing behind ("couldn't make LG Test 2: …; nothing was left
half-made"). A folder with no copy of the server program, in a Huragok folder with
none either, gets a hard link to the program that made it (a copy where the drive
can't link).

Project Reclaimer 0.10.8's `init` makes **two servers** in the folder, on ports
49176 and 49177, with its master on 49175. On most hosts those are taken, so N
says which ports are in use and by what, then offers three **quick actions**, each
shown line by line before it's written (Esc leaves the folder exactly as `init`
made it):

1. **One server only**: keep the first `[[server]]` block, named as you named the
   folder, and take out the others (`--one-server`).
2. **Free ports**: a free game port for each server, and the folder's master off,
   or on a free port (`--free-ports`, with `--master off` or `--master free`).
   "Free" means nothing on this PC listens on it (TCP or UDP) and no server
   Huragok knows uses it, disabled and left-out ones too.
3. **Add the console**: a random 24-character `[rcon] password`. The console
   listens on its game port's number; only when that's taken is an `rcon_port` of
   its own written (`--console`).

**A** (or `--all`) does all three in turn. On the command line each is shown and
asked; `--yes` writes them without asking (still shown). The folder is picked up
within seconds once it has its name, and started once its ports are free on this
PC. **G** then shows what it still needs: usually Halo 3's files (**I**, or
`start.cmd repair`: a server without them is held, not started) and a playlist of
your own.

A new server's **name** becomes its folder's name too, so it's 1 to 48 characters,
none of `< > : " / \ | ? *`, no invisible or control characters, not ending in a
space or a dot, not starting with `.` or `_` (folders Huragok ignores), and not a
name Windows keeps for a device (`CON`, `NUL`, `COM1`…). A name already used by any
server here, disabled or not, is refused, and so is one that only differs in
punctuation or capitals ("LG Slayer", "LG-Slayer"): the two would share their
records and logs.

Packages, the eight starter packages and `start.cmd package` are gone since 0.7.0
(the starters' playlists are kept in `engine/tools/archive/starter-packages`). A
`PACKAGES/` folder is left where it is. To start a server like one you have, copy
its playlists and content files over (**G → 1 → C** copies a playlist, checked by
the other server's own program first).

The server program itself (`project-reclaimer.exe`) is found in each server folder,
or **one copy at the root** serves every server. A command for one server folder
runs that folder's own copy (servers that update themselves move ahead of the
root's); the rest (making a folder, checking a custom game) use `[setup] program`
when it names one, or else the newest copy here, by its `--version`.

## What a server plays: G in the window

**G** picks a server and opens its home: what its own file says it plays
(`playlist = "playlists/races.playlist.json"`, or `playlists = [...]` by lobby
size, a game, a campaign), what's on now and next (the game the last ballot picked,
a game set from the console, players voting), and its **file health**: every
warning about its playlists, game modes, layouts and mods, refused first. It reads
everything the server can play (`dedicated content`, the mods in its content
folder, its Workshop items) and shows the counts. Nothing here changes what it
plays: playlists and custom games are made by hand, and what a server plays is set
in its `dedicated.toml`. Then:

1. **Playlists**: every playlist in its `playlists/` with its games, and which one
   its file plays (`◄ in use (playlist =)`, or the lobby sizes it's for). Then:
   - **S** shows one as the server reads it (the program's own `playlist show`):
     its games, weights, vote time, teams, and what each game changes;
   - **H** history: every version Huragok kept, newest first, with what each holds,
     how it came (a change made by hand found the next time Huragok looked, a copy
     from another server, a version put back) and where from; **put one back**,
     checked first by the server's own program. They're in
     `records/playlists/<server>/`;
   - **P** picks: each of its games with how often players picked it on the ballots
     and its share, the ones never picked marked, counted on every server that
     plays a playlist of that name;
   - **C** copies one, exactly as it is, to other servers you pick. Each copy is
     checked first by that server's own program against its own content. When it
     refuses, its words are shown with what copying anyway does (a playlist the
     other server plays would be refused, and it keeps playing the old one), and
     you're asked once. The version a copy replaces goes in that server's history.
     A copy is never played by itself: the other server's file names what it plays;
   - **F** opens one in your editor.
2. **Game modes**: the saved game types in `content/Game Modes`: `.json` game types,
   `.rmeg` scripted modes (Project Reclaimer 0.10) and MCC's `.bin` variants,
   listed by name beside the built-in game types (a playlist, `game =` or
   `variant =` names one). **S** shows what one changes; **O** copies one to other
   servers, checked first by each one's own program (`mode-check`; asked once when
   it refuses); **D** sets one aside: moved to `content set aside` beside the
   content folder, never deleted. A server that plays it is named first, with what
   setting it aside does (a server whose game file is gone doesn't start).
3. **Maps and layouts**: numbered lists of each game's maps, the mod maps, saved
   maps and map variants (Forge layouts), campaign canvases, game types and game
   variants, the mods (what each brings) and the Workshop items. Long lists take
   words to narrow them (`mlg`, `hardcore`). The first name on a line is what
   playlists and `map =` / `game =` take; `[brackets]` show the file reference the
   server wants when two names collide.
4. **Mods**: the SteamCMD status, the `[workshop] items` of its `dedicated.toml`
   with **what the server last said about each** ("ready", "installed", an error,
   or "its map was skipped: Map needs matching .mapinfo metadata", which is the
   item's own packaging; for a map file the server couldn't read, such as "its tag
   index is damaged", Huragok reads the file itself and says which part is broken
   and which Workshop upload it is, to pass on to its author), the mods installed,
   and **whether each mod is good to go** (see "Is each mod good to go?" in
   [managing.md](managing.md#is-each-mod-good-to-go)). `[workshop] items` is yours
   to write: the server fetches what it lists and keeps it up to date by itself.
   Then **I** fetches them now (a running server fetches its own: Huragok follows
   its console instead of running a second SteamCMD beside it; a stopped one gets
   `dedicated mods`, with Esc to cancel), **L** copies a mod's Forge layouts (its
   `Project Reclaimer Layouts` folder, which the server never reads by itself) into
   `content/Maps/<mod>/`, where the server lists them as saved maps (only when you
   ask; after the mod's next version, L brings its new ones, and a layout you
   changed there is left as it is), **S** sets SteamCMD up, **M** lists the mod
   maps the server lists.
5. **Check it**: the server's own `dedicated check`, read-only as far as Huragok
   goes (the program's check fetches missing Workshop items itself).
6. **Import from MCC** (or any folder or file). MCC ships every matchmaking variant
   343 made for Halo 3 (`halo3/hopper_map_variants`, 320 `.mvar`: MLG, Team
   Hardcore, Infection, Grifball…; `hopper_game_variants`, 334 `.bin`), but a
   server lists only what's in its content folder. This copies them into
   `content/Maps/From MCC` and `content/Game Modes/From MCC`, optionally only the
   names containing your words (`hardcore`, `mlg`…); the server picks them up while
   running and lists them by name. Saved maps and layouts (`.json`) go into Maps,
   saved game types and `.rmeg` modes into Game Modes. Command line:
   `content -s <server> --import [--from <folder>] [words]`; MCC is found where
   `[setup] mcc_folder` or Steam's own records say it is.

**F** opens its folder, **E** its `dedicated.toml` in your editor, and **X**
dismisses a warning for this server until the file it's about changes (for good,
for one about no file).

A playlist and a custom game copy are written as a new file moved into place, so a
hard-linked copy in another server's content folder keeps its own version.

### Playlists by player count
Since Project Reclaimer 0.10.1 the server switches playlists by lobby size itself,
with no reload, from its own `[[server]]` block, in place of `playlist = …`:

```toml
playlists = [
  { file = "playlists/small.playlist.json", players = "1-10" },
  { file = "playlists/big.playlist.json", players = "11+" },
]
```

1 to 16 entries, each a file and the lobby sizes it's for (`"8"`, `"8-20"`,
`"12+"`). Huragok reads it ("playlists by lobby size: small 1-10, big 11+" on G and
the status card), and the file health checks every playlist it names.

Huragok's own switcher (G → P, 0.5.x to 0.6.x) is gone. A server that used it keeps
its `playlists-by-players.toml`, which is no longer read; Huragok says so once per
start, on its card and in `check`, with the same steps written as the server's own
`playlists = [...]`, ready to paste into its `[[server]]` block.

### File health: what will probably fail, and why
For each playlist, every game's map, game mode, base map and preview image is
looked up: found (where), missing, or there but the server will skip it (and why).
The server's own check runs on a copy of its files after each change, and what it
refuses is said in its words. Huragok's own judgements are warnings, never
refusals, since a guess can be wrong after a Project Reclaimer update: too many mod
maps in one playlist, two mod maps with one map ID, a layout made for another
version of its mod, a saved map past 9,999 objects, options on a Combat Evolved or
Halo 2 map, a label the server's version doesn't take. A game the server skips
while it plays ("its map failed to load…") is said too. **"Who uses this file?"**
sees every way a server can play one (`playlist`, `playlists`, a custom game, a
co-op game, `.rmeg`), so a file a server plays is never set aside or replaced
without a word.

Each warning is one of four kinds, marked on G:

| Mark | Kind | Means |
| --- | --- | --- |
| ✗ | Refused | the server's own program said no (`check`, a reload, a start) |
| ! | Seen | what the server said while it played (a game skipped) |
| ? | Check | Huragok's own judgement of something the server lets through |
| i | Info | nothing wrong, worth knowing |

They're worked out as Huragok starts, whenever a file a server plays from changes,
after a reload and after a mod update, and shown on G, beside each setting on D,
on the status card (V), in `huragok.log` and in `start.cmd check`. The words come
from one catalogue kept as data (`templates/warnings.toml`), each rule with the
newest Project Reclaimer version it was checked on: a server on a newer version
gets one line saying so, and its own check still applies.

## Check: C
**C** runs the server program's own `dedicated check` for one server or every
server (game files, settings, playlists, maps, ports; it installs missing Workshop
items), then offers what the check found Huragok can do. Files in the content
folder that aren't MCC variant files (the server skips them at every start) can be
moved to `content set aside`. The firewall rules it prints (`New-NetFirewallRule
…`) are run in an administrator PowerShell (Windows asks first), each under a name
of its own (`Reclaimer games UDP 49176-49177`) and only when it isn't there, so
running them again changes nothing; when the administrator prompt is declined or a
rule fails, it says so ("Windows Firewall wasn't changed: …") rather than calling
it done. After an MCC update, when the check says Halo 3's engine file doesn't
match, it copies `halo3.dll` from your MCC folder, and `halo3odst.dll` too when the
game folder has ODST's files (each old one is renamed, since a running server holds
it); the screen names every server that shares that game folder, and a game folder
that is MCC's own (linked to it) is left alone, since its file is MCC's. With no
MCC on this PC, it offers to download Halo 3's files with SteamCMD (I sets it up)
and copies the new engine file in from them the same way. When the check says
Halo 3: ODST's files aren't in the game folder, it offers to add them from MCC.

## SteamCMD: I in the window
**I** (or `start.cmd steam`) shows SteamCMD and the Steam account, then every game
folder here: its Halo 3 multiplayer maps, the campaign files, ODST's, and which
servers use it, and any server missing files. Then:

**1 SteamCMD and the Steam account.** A server downloads Workshop items itself, but
only with SteamCMD and a Steam account that owns MCC: an anonymous SteamCMD sign-in
can't fetch MCC's items (checked). It:

- finds SteamCMD: `[setup] steamcmd`, `steamcmd/` at the root, `C:\steamcmd` (the
  folder the server's own template names), your PATH, `~/steamcmd` on Linux;
- **installs it** when missing, from Valve's installer, into `steamcmd/` at the root
  (kept with Huragok) or the standard folder, and runs it once so it updates
  itself (`steam install [--standard] [--path]`);
- **signs the account in**: it opens SteamCMD in a window of its own where Steam asks
  for the password and the Steam Guard code **once**; after that the sign-in is cached
  and the servers fetch items without prompts. No password is ever written anywhere
  (`steam login <account>`). The account is the name you sign in to Steam with, not
  your profile name: 2 to 64 letters, digits and underscores, so anything else is
  refused before it reaches a command line. Esc at "Press Enter once the sign-in
  window says it's done" leaves everything as it was: nothing is written until the
  sign-in is done;
- writes `[setup] steamcmd` and `steam_username` into `settings.toml`, and offers
  to write the SteamCMD lines into one server's `dedicated.toml` (below);
- can put SteamCMD's folder on your PATH for other tools (`steam path`).

**2 Halo 3's files, 3 the campaign missions, 4 Halo 3: ODST's files**, and on Project
Reclaimer 0.10 **5 Halo: Reach's, 6 Reach's campaign, 7 Halo: Combat Evolved's, 8 Halo
2's** (from MCC only for now), into this folder's shared `game/`, beside what's
there (running servers keep running). Each lists every way there is, with sizes,
before asking:

- **copy them from MCC on this PC**: hard links on the same drive, so no space used.
  MCC is found only where Steam's own records say it is (the registry's Steam folder,
  the libraries `libraryfolders.vdf` lists, MCC's `appmanifest_976730.acf`); no drive
  is searched. MCC that Steam doesn't list: name it as `[setup] mcc_folder`;
- **download them with SteamCMD**, as the signed-in account. MCC on Steam comes in
  parts (depots), and a server needs only its game's: Halo 3 multiplayer is 9.3 GB
  (`halo3.dll`, `shared.map` and the 27 maps; 5.8 GB kept, since sound is left out),
  the campaign 9.0 GB, ODST 8.7 GB. Never all of MCC (57 GB with every game).

With a Huragok window running, the getting runs in the background and the window stays
free: the log says when they're in, and the servers waiting for them start. Without the
window: `start.cmd steam files [halo3|campaign|odst|reach|reach-campaign|halo1|halo2]
[--download]`.

**L SteamCMD lines for a server**: `[workshop] steamcmd` and `username` (this PC's
SteamCMD and I's account) written into **one** server's `dedicated.toml`, shown
before they're written (`start.cmd steam lines "<server>" [--yes]`). Never into
every server at once. A server's `[workshop]` takes effect when its `dedicated`
restarts.

**R Repair a server's folder** (`start.cmd repair [servers] [--yes]`): what its
folder lacks, listed first, and put right only when you say yes, a copy of its
`dedicated.toml` kept before the first change:

| What's missing | What repair does |
| --- | --- |
| Halo 3's files (or the campaign's, ODST's, Reach's, CE's, Halo 2's, for what it plays) | points its `game` at a game folder here that has them, or gets them into Huragok's `game/`: copied from MCC on this PC, otherwise downloaded with SteamCMD |
| its ban list, named outside its folder and not on this PC | an empty one of its own, `bans.json` in its folder (Huragok's own bans and admins are put on it as it connects) |
| a ban list it shares with other servers | a copy of its own, every ban, admin and VPN allowance kept |
| its content folder, named outside its folder and not on this PC | an empty content folder of its own |
| a `[workshop] steamcmd` that isn't on this PC | this PC's SteamCMD in its place |
| Reach's game and map variants (0.10.0's `init` left them out) | copied from MCC on this PC |

Huragok gets none of this by itself. A server missing its game files is warned
about and held (it can't start without them), naming the fix, instead of failing
its starts until it's FAULTED; once I or repair has put the files in, it starts.

**Other games.** A server that plays Halo 3: ODST needs ODST's files beside Halo 3's
(about 3.5 GB) and holds 16 players at most. **Halo: Reach, Halo: Combat Evolved and
Halo 2** play on Project Reclaimer 0.10 and later: Reach's multiplayer and Firefight
maps (7 GB; its campaign too, 11 GB), Combat Evolved's (3.3 GB) and Halo 2's (3.2
GB), from MCC on this PC only for now. Halo 3's files are always needed: every
game's maps are built on Guardian. A playlist may mix every game's maps; Combat
Evolved's and Halo 2's maps take only their own game types, and Reach's take Halo
3's or Reach's own variants. A server whose maps need a game its game folder lacks
is warned about the same way; I or repair adds it.

From the command line: `content [-s server] [--import] [words]`, `playlist [-s
server] [list | show <name>]`, `variant [-s server] [list | show <name> | check
<name>]`, `workshop [-s server] [list | install]`, `repair`, `steam lines
<server>`, and `dedicated [-s server] <any dedicated command>` for the rest
(`start.cmd dedicated -s Slayer vpn`, `start.cmd dedicated -s Slayer bans`).
`playlist new`, `playlist use`, `workshop add` and `workshop remove` are gone: they
say what to write in the file instead.

## Editing a server's files
You change a server by editing its files: **O** opens any of them (a server's folder,
its `dedicated.toml`, its own `intervalmessages.txt`, each playlist, its ban list,
its saved game types and `.rmeg` modes), and so do **G → F / E** and **D → O**. They
open in Notepad (Windows) or the desktop's editor (Linux). Since Project Reclaimer
0.9.18 the server re-checks `dedicated.toml`, the playlists and the content folder a
couple of seconds after a save (`auto_reload = true`): most settings apply at once,
`friendly_fire`, `team_changing` and a playlist from the next game or vote, ports
and RCON settings by a restart once the server is empty. **The result shows in the
dashboard's events**: `settings reloaded: max_players 16 → 12`, or `settings NOT
reloaded, nothing changed: …` with what's wrong, and the file health on G follows
the change within seconds. **D** shows a server's `dedicated.toml` read-only, each
setting with what it does and the warnings beside it
([managing.md](managing.md#a-servers-settings)).

## A server folder from somewhere else
Any folder in `SERVERS/` with a `dedicated.toml` in it is a server folder: drop one
in (made by hand with `dedicated init`, copied from another PC or another Huragok
folder) and it shows on the dashboard once its `dedicated.toml` has been left alone a
minute (so a folder still being edited isn't started half-made). Each `[[server]]` block in it
is a server of its own, with its own row, started, stopped and restarted on its own.
Huragok changes nothing in it; what it lacks is a warning, and the server is still
listed and started where it can be:

- **No console** (no `[rcon]` password): it's started and kept on all the same,
  watched through its `status.json` (up or not, its game, who's on), restarted after
  a crash or a freeze and after the game as usual. Huragok can't tell its players
  anything or moderate it until a password is added under `[rcon]` (by hand, or
  **D → Q** adds one, shown first); the server uses it from its next start.
- **Game files, a content folder, a ban list or a SteamCMD that aren't on this PC**
  (its `dedicated.toml` points at what it had where it came from, such as `game =
  "../../game"`): warned about with the fix, and put right only when you ask (I →
  R, `start.cmd repair`). A server without its game files is held until they're
  there. A missing ban list matters: the program reads it as an empty one, so the
  bans it had stayed where the folder came from (copy that `bans.json` over to keep
  them).
- **A port another server here uses**: the one Huragok already looks after keeps it,
  and the other is left off, and said, until one changes. Before every start the
  port is also checked against everything on this PC, UDP too, not just Huragok's
  own servers.
- **The same name in two folders**: the one Huragok already looks after keeps
  running, and the other is left off, and said, until one is renamed (settings,
  records and stats are kept by name).
- **A server something else already runs** (started by hand, or by another tool):
  before every start Huragok reads the server's own `data/<server>/supervision.json`.
  When the program's supervisor for it is running and Huragok didn't start it,
  Huragok watches and moderates it instead (a second copy would crash-loop and wipe
  its `status.json` and log). It restarts such a server only through its own
  console's `restart`, for a mod or release update, and never stops, starts or kills
  it.

A copy also keeps the original's `data/stats-key.json`, its identity to the stats
service: two copies running at once report as one server.

**A server renamed in its `dedicated.toml` stays the same server**: the same folder
and game port (or the same place in the file, when the port changed too). Huragok's
own settings for it, its records, stats, leaderboards and history follow it to the
new name, and the log and window say `renamed X → Y`; lines already logged and cards
already posted keep the old name. Its folder isn't touched. A running server is
followed as soon as it stops: its next restart does it. `follow_renames = false` in
`settings.toml` turns this off (it starts afresh). **D → N** still renames
everything at once when you ask: the block, its data folder, Huragok's records and
the folder.

## Folders Huragok ignores
A folder in `SERVERS/` whose name starts with `.` or `_` is never a server:
Huragok makes new ones as `.new-<file name>` (`.new-lg-test-2`) and you can set a
server aside by renaming its folder to `_Old Slayer`.
