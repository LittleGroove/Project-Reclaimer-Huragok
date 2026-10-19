# Setting servers up

Setting servers up is Huragok's own work, the Engineer's (its lines carry the
`HRGK` tag). It makes and changes server folders with the server program's own
`dedicated` commands, so everything it writes is what the Project Reclaimer
program itself accepts. Nothing here needs a server stopped: the servers keep
running while you make a new one, change a playlist or install a Workshop item.
Work that takes a while (making a server, installing SteamCMD, reading a server's
catalogue) says so on the screen and ignores keys pressed meanwhile, so a stray Esc
or Q never answers the next question; the servers' care goes on throughout.

Every change Huragok makes to a `dedicated.toml` or `settings.toml` is checked
before the file is replaced: the change is made in memory and read back, and only
if exactly that change differs is the file written (in one step). Otherwise the
file is left byte for byte as it was, and the message says to edit it by hand
(**E**). Settings written in a form the editor won't change (a dotted key such as
`workshop.items = …`, an inline table such as `rcon = { … }`) are refused that way,
without showing their value.

## A new server: N in the window, or `new`

```text
start.cmd new "Infection Night" --package infection
start.cmd new "Team Slayer 2" --package team-slayer --port 49180 --hidden --password letmein
start.cmd new "Rockets" --map Guardian --game rockets --max-players 8
```
(Linux: `./start.sh new …`.) The window's **N** asks the same things: a package, the
name, the port (the next free one is offered), hidden or not, a join password, the
player cap. Then it:

1. makes `SERVERS/<name>/` with `dedicated init` (as `SERVERS/.new-<its file name>/`,
   such as `.new-infection-night`, until it's done, so a half-made folder is never
   started; a build that fails part way leaves nothing behind: "couldn't make
   Infection Night: …; nothing was left half-made");
2. points it at the **root's shared folders** (below), or copies Halo 3's files from
   MCC when nothing can be shared or linked;
3. copies the package's playlists and content in, writes the `[[server]]` block (name,
   port, what it plays, your choices), a made-up RCON password, and the `host_name`,
   `stats_server`, `public_address` and `upnp` of the server folder you already have;
   sets `auto_update = true`, so it installs new Project Reclaimer releases itself,
   and gives it a copy of the program of its own for that: a hard link to the
   program that made it (no space used), copied only when the drive can't link
   (`[setup] auto_update = false`, or `new --no-auto-update`, leaves the updates to
   Huragok's U instead; see [managing.md](managing.md#updates-u));
4. writes the SteamCMD account **I** set up into its `[workshop]` table and installs
   the package's Workshop items with `dedicated mods`;
5. runs `dedicated check`. If it passes, it says `Made <folder>. Feet first into hell.`,
   and the server appears on the dashboard and starts (unless `--no-start`). If not,
   the folder stays, **autostart off** (written before the folder appears, so it's
   never started meanwhile), and the problems are shown: fix them, then **C** (or
   `start.cmd dedicated -s "<name>" check`) and **S**.

What a new server can be:

- **its name**: 1 to 48 characters, which become its folder's name too, so none of
  `< > : " / \ | ? *`, no invisible or control characters, not ending in a space or a
  dot, not starting with `.` or `_` (folders Huragok ignores), and not a name Windows
  keeps for a device (`CON`, `NUL`, `COM1`…). Two names that only differ in
  punctuation or capitals ("LG Slayer", "LG-Slayer") would share their records and
  logs, so the second is refused;
- **its port**: 1024 to 65535, not one another server here uses (disabled servers
  and folders Huragok left out count too, and so does a discovery master's), and
  nothing on this machine may be listening on it. N asks again for one out of range;
- **its players**: 1 to 127, at most 16 for campaign co-op and Halo 3: ODST.

A name already used by any server here, disabled or not, is refused too.

### What new servers share
By default a new server carries its own `dedicated.toml`, `playlists/`, `data/` and
**`content/`** (its maps, game types, mods and images). The big parts live at the root
once, for every server made this way:

| At the root | What | Made when |
| --- | --- | --- |
| `game/` | Halo 3's files (5.5 GB): **hard-linked** from the first server folder that has them (no space used on the same drive), or copied from MCC with `dedicated init --from` | the first new server |
| `bans.json` | one ban list, so bans, console-added admins and VPN allowances reach every server at once (what the server program expects: "every server sharing the ban list") | the first new server |
| `content/` | only when `[setup] share` names it: one content folder every server plays from | the first new server |

`[setup] share` in `settings.toml` picks which (`["game", "bans"]` by default; add
`"content"` to share one content folder; `--own` on the command line gives a server its
own copies of everything). A server with its own content folder gets its Workshop mods'
new versions after its own games, and its custom games stay its own; a server sharing
one waits until nobody is on any server using the mod (the program swaps a shared mod
for all of them at once). **D → its own content folder** gives a server that shares one
a copy of its own (hard links on the same drive, so no space is used; an old unused
`content/` in its folder is set aside, never deleted). Servers you made by hand keep
their own folders; nothing moves.

Halo 3's files come from one of: a server folder that has them (linked), your MCC
folder (`[setup] mcc_folder`, or `--from "<folder>"`; `--from` alone means Steam's
default folder; `--maps "Guardian,Valhalla"` copies some maps only), or
`--link-from "<server folder>"`. A server made with `--own` and no MCC folder (no
`--from`, no `[setup] mcc_folder`) gets its own `game/` hard-linked from the root's
shared one, or else from the first server here that has Halo 3's files; never from
MCC's own folder, which would link every game MCC has. A path can be typed or pasted
in quotes, as Explorer's "Copy as path" gives it, here and everywhere Huragok asks for
one.

The program itself (`project-reclaimer.exe`) is found in any server folder, or **one
copy at the root** serves every server (they must all be on the same version anyway).
A command for one server folder runs that folder's own copy (servers that update
themselves move ahead of the root's); the rest (making a folder, checking a custom
game) use `[setup] program` when it names one, or else the newest copy here, by its
`--version`.

## Packages: a server in a box
A package is everything that makes a server *that* server, minus Halo 3's files and
the data: a folder in `PACKAGES/` (zipped to share):

```text
PACKAGES/infection/
  package.toml
  playlists/infection.playlist.json
  content/Game Modes/…  content/Maps/…  content/Images/…     (optional)
```
```toml
name = "Infection"
description = "Infection, Alpha Zombies and Flood Infection on the maps made for it"
version = 1
needs_maps = ["Last Resort", "Sandtrap", "Ghost Town"]   # Halo 3 maps the server must have
needs_campaign = false                                    # the campaign missions too
needs_odst = false                                        # Halo 3: ODST's files too

[server]                       # goes into the [[server]] block
playlist = "playlists/infection.playlist.json"
max_players = 16

[defaults]                     # optional: the folder's [defaults]
voice_proximity = false

[workshop]
items = []                     # Workshop item IDs, installed when the server is made
```
In `[server]`, a `map` or `game` file is named inside the content folder
(`game = "Game Modes/snipers.bin"`), and a saved custom game (`variant`) inside the
package's own folder (`variant = "content/Game Modes/snipers.json"`), which is
pointed at the new server's content folder as the package is applied. Applying a
package never replaces a content file that's already there (other servers may use
it): the notes name any whose package copy differs. A package with a mistake in its
`package.toml` (a field of the wrong type, say) is listed as broken with the reason;
the others list as usual.

Host-specific things stay out (name, port, passwords, admins, `host_name`):
Huragok asks for them. **Seven starter packages** are written to `PACKAGES/` on the
first start (never replaced after that; edit them freely): *Team Slayer*, *Big Team
Battle*, *MLG Hardcore* (Halo 3's own MLG map variants and Hardcore game variants),
*Infection*, *Scripted Modes* (Gun Game, One in the Chamber, Growth, Zone Control,
UNSC vs Covenant), *Campaign Co-op* (needs the campaign files:
`dedicated init --from "<MCC>" --maps campaign`) and *ODST Firefight* (Project
Reclaimer 0.9.19 and later: Firefight on Crater (Night), Heroic). A root made before
0.4.0 has no ODST Firefight starter; `package save` makes one from an ODST server.

A server that plays Halo 3: ODST (`odst = { … }` in its block, or a package with
`needs_odst = true`) needs ODST's files beside Halo 3's, about 3.5 GB. Huragok adds
them to its game folder (the root's shared `game/` too) as it makes the server,
from the MCC on this PC that has ODST installed (`[setup] mcc_folder`, or Steam's
folders), with `dedicated init --maps odst` into a scratch folder, then hard links;
what's already there stays as it is. Without such an MCC the server is made all the
same, with a note saying how to add them. An ODST server holds 16 players at most.

```text
start.cmd package list                      what's in PACKAGES/
start.cmd package show infection
start.cmd package save "Infection Night" "My Infection" --description "the Friday setup"
```
`save` (also **G** then **5** in the window) turns a server you have into a package:
its `[[server]]` block, its playlist, the content files and Workshop items the
playlist names. That's how you clone a server, or hand a setup to another host.

## What a server plays: G in the window
**G** picks a server, reads everything it can play (`dedicated content`, the mods in
its content folder, its Workshop items) and shows the counts: `27 maps, 1 mod maps,
57 map variants, 10 game types, 55 game variants, 1 mods, 0 Workshop items`. Then:

1. **browse what it can play**: numbered lists of Halo 3's maps, mod maps, map
   variants, game types, game variants, the mods (what each brings: its maps, its
   Forge layouts, its Workshop ID) and the Workshop items. Long lists take words to
   narrow them (`mlg`, `hardcore`) and come a page at a time. The first name on a line
   is what playlists and `map = ` / `game = ` take; `[brackets]` show the file
   reference the server wants when two names collide.
2. **playlists**: every playlist with its games and which one is in use, then
   - **build one from numbered lists**: name, vote seconds, then pick maps by number
     (Halo 3's, mod maps, variants), then game types or variants by number; every
     pair becomes a game; leave out the pairs you don't want, set weights
     (`1=3 4=2`), and it's checked with `dedicated playlist show`, saved, then
     offered to the server;
   - **build one from a mod's maps**: the same, but the maps come from the installed mod
     maps, written the way the server names them (`workshop:<item>/<map>`), so the
     right mod is found (a playlist takes up to 23 mod maps from 16 mods);
   - **edit one**: add games (pick maps and game types), remove by number, weights,
     vote seconds, rename, save and check, and **change one game**:
     - the **name** players see ("BR Starts"), its **weight**;
     - its **rules and options**, laid out like the game's own Game Options screen:
       the game type's rules (score to win, points, its own rules such as Flag type or
       Hill movement), Rounds & teams, Respawn, Powerup durations, Map & equipment
       (weapon set, vehicles…), Traits by who gets them (every player, after
       respawning, powerups, the flag carrier, the infected…) in Shields & health,
       Weapons & damage, Movement, Sensors and Appearance, Team characters, and World
       (sprint). Every choice is listed numbered with its label; a number shows its
       range; R puts back the game type's own value. Choices are written by their
       label (`"player.primary_weapon": "Battle rifle"`), so the file stays readable;
       the one exception is the character set, written as its number, because its
       label differs between program versions ("Vanilla characters" before 0.9.19,
       "Halo 3" from it) and every version takes the number. As it starts a server,
       the Superintendent also puts any label in its playlists the way that server's
       program calls it.
       A game variant's own game type is known from the listing, so a `.bin` such as
       `2V2 HARDCORE TS` gets Slayer's options;
     - its **vote image**: a numbered list of `content/Images`, or import a PNG or
       JPEG (4 MiB at most; 32 different ones per playlist);
     - its **base map** (a Forge map made on a mod map), **anti-cheat off** for that
       game, or **take out every change**.
   - **show** one as the server sees it, **use** one for this server (applies from
     the next vote; its `map`, `game`, `variant`, `campaign` or `odst` is taken out
     in the same write, since the program won't start a server that has both),
     **open** one in the editor.

   Every playlist is checked by the server program as a draft (`dedicated playlist
   show` on a copy beside it) before it's saved. When the program can't play some of
   its games, they're named and can be dropped. A playlist a server plays is never
   replaced by one it refuses: the choice is **Leave it unsaved** (the server keeps
   the one it has); one no server plays can be kept as it is, to fix with **E**.
3. **Workshop items and mods**: the SteamCMD status (is it installed, which account,
   how many servers are set up for it; **S** opens the setup), the `[workshop] items`
   of its `dedicated.toml` with **what the server last said about each** ("ready",
   "installed", an error, or "its map was skipped: Map needs matching .mapinfo
   metadata", which is the item's own packaging), the mods installed with their maps
   and Forge layouts, and **install a mod's Forge layouts**: a Workshop item often
   ships saved Forge maps in a `Project Reclaimer Layouts` folder that the server
   never installs by itself; this copies them into `content/Maps/<mod>/`, where the
   server lists them as saved maps.

   **How items get fetched.** A *running* server fetches new items itself a couple of
   seconds after `[workshop] items` changes (auto_reload); the screen then follows its
   console and shows the fetch (`Workshop 3820687243: acquiring from Steam … ready`),
   Esc stops watching while the server keeps going. Nothing else should run SteamCMD
   beside it: a second one would wait on the first, which is why **fetch now** on a
   running server only watches. On a *stopped* server, **fetch now** runs `dedicated
   mods` with its output as it comes and **Esc cancels** it. The dashboard's events show
   the same Workshop lines whatever screen is open. A change to `[workshop] steamcmd` /
   `username` takes effect when the server's `dedicated` restarts (**R**).
4. **check it**: `dedicated check` (also **C** on the dashboard, for one or every
   server: game files, settings, playlists, maps, ports; it installs missing Workshop
   items).
5. **save it as a package**: its block, its folder's `[defaults]` (not the join
   password or admins), its playlist and the content the playlist and its variant name.
6. **open its folder**.
7. **import map and game variants from MCC** (or any folder or file). MCC ships
   every matchmaking variant 343 made for Halo 3 (`halo3/hopper_map_variants`,
   320 `.mvar`: MLG, Team Hardcore, Infection, Grifball…; `hopper_game_variants`,
   334 `.bin`), but a server lists only what's in its content folder. This copies
   them into `content/Maps/From MCC` and `content/Game Modes/From MCC`, optionally
   only the names containing your words (`hardcore`, `mlg`…); the server picks them
   up while running and lists them by name, and playlists can name them. Saved
   `.json` maps and game types from a download go to the folder they belong in.
   Command line: `content -s <server> --import [--from <folder>] [words]`; MCC is
   found by itself when it's installed on this PC (`[setup] mcc_folder`, then Steam's
   own list of library folders, then Steam's default folder, then the PC's fixed
   drives).
8. **reload the listing** after changing files by hand.
9. **custom games** (game variants): a game type with rules of its own, saved as a
   `.json` in `content/Game Modes`, listed by its name beside the built-in game types
   (playlists and **D → What it plays** can name it). **New**: pick the game type
   (numbered), name it, then the same rules-and-options screens as a playlist game;
   **copy** one (the shipped scripted modes too: their options are editable, their
   scripts are files for **E**), **edit**, **show** what one changes, **play** it on
   this server, **delete** (refused while a server plays it: give it something else
   to play first). Before saving, the server program checks it (`mode-check`); the
   file is written whole and moved into place, so a hard-linked copy in another
   server's content folder keeps its own version.
   `start.cmd variant [list|show|check] [name]` does the same from a terminal.

**C** (check) offers what the check found Huragok can do: the firewall rules it prints
(`New-NetFirewallRule …`) are run in an administrator PowerShell (Windows asks first),
each under a name of its own (`Reclaimer games UDP 49176-49177`) and only when it isn't
there, so running them again changes nothing; when the administrator prompt is
declined or a rule fails, it says so ("Windows Firewall wasn't changed: …") rather
than calling it done. After an MCC update, when the check says Halo 3's engine file
doesn't match, it copies `halo3.dll` from your MCC folder, and `halo3odst.dll` too
when the game folder has ODST's files (each old one is renamed, since a running
server holds it); the screen names every server that shares that game folder, and a
game folder that is MCC's own (linked to it) is left alone, since its file is MCC's.
With no MCC on this PC, it offers to download Halo 3's files with SteamCMD (I sets it
up) and copies the new engine file in from them the same way.
When the check says Halo 3: ODST's files aren't in the game folder, it offers to add
them from MCC.

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
- **tells every server**: `[workshop] steamcmd` and `username` go into each server
  folder's `dedicated.toml` (a server's `[workshop]` takes effect when its `dedicated`
  restarts), and `[setup] steamcmd` / `steam_username` into `settings.toml`;
- can put SteamCMD's folder on your PATH for other tools (`steam path`).

**2 Halo 3's files, 3 the campaign missions, 4 Halo 3: ODST's files**, into this
folder's shared `game/`, beside what's there (running servers keep running). Each
lists every way there is, with sizes, before asking:

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
window: `start.cmd steam files [halo3|campaign|odst] [--download]`.

**Huragok does this by itself, too**, for a server that needs the files: see "A server
folder from somewhere else" below.

From the command line: `content [-s server] [words]`, `playlist [-s server] [list |
show <name> | new <name> --maps … --games … | use <name>]`, `variant [-s server]
[list | show <name> | check <name>]`, `workshop [-s server] [list | add <ids…> |
remove <ids…> | install]`, and `dedicated [-s server] <any dedicated command>` for
the rest (`start.cmd dedicated -s Slayer vpn`, `start.cmd dedicated -s Slayer bans`).

## Editing a server's files: E
**E** lists Huragok's four files (`settings.toml`, `commands.toml`, `automod.txt`,
`names.txt`), then each server: its `dedicated.toml`,
each playlist, its ban list (or the shared one), `disabled-mods.json`, saved game
types. They open in Notepad (Windows) or the desktop's editor (Linux). Since Project
Reclaimer 0.9.18 the server re-checks `dedicated.toml`, the playlists and the content
folder a couple of seconds after a save (`auto_reload = true`): most settings apply at
once, `friendly_fire`, `team_changing` and a playlist from the next game or vote, ports
and RCON settings by a restart once the server is empty. **The result shows in the
dashboard's events**: `settings reloaded: max_players 16 → 12`, or `settings NOT
reloaded, nothing changed: …` with what's wrong.

## A server folder from somewhere else
A server Huragok made shares this folder's `game/` and `bans.json`: its
`dedicated.toml` says `game = "../../game"` and `bans = "../../bans.json"`. Copy or move
just that server folder into another Huragok folder (or onto another PC) and those
paths point at nothing. The server program then refuses to start ("missing files
copied from your own MCC installation"), and it reads a missing ban list as an empty
one, so the bans the server had are gone without a word.

Huragok puts that right by itself, as soon as it finds the folder and again before each
start (with `backend = "process"`; in a container the paths aren't this PC's):

| What's missing | What Huragok does |
| --- | --- |
| the ban list | makes an empty one where the file says; when even its folder isn't on this PC, points the server at this folder's shared `bans.json` (made if need be). The log says so, and that the old bans stayed where the folder came from: copy that `bans.json` over this one to keep them |
| the content folder | makes it, with `Maps`, `Game Modes`, `Images` and `Mods` |
| Halo 3's files | points the server at a game folder here that has them (this folder's `game/` first). With none, gets them into `game/`: copied from MCC when it's on this PC, otherwise downloaded with SteamCMD (`[setup] download_game_files`, on by default; false downloads nothing without you). The server shows **WAITING** and why until they're in, and its failed starts aren't counted. With no way to get them it waits and says to press I |
| the campaign missions, ODST's files | the same, for a server that plays the campaign or ODST |
| a map the copy left out | a start that fails for missing files gets them before it tries again |
| `[workshop] steamcmd` from another PC | this folder's SteamCMD (the one I set up) in its place |
| its port, held by the copy it was made from | it waits ("in use by something that isn't this server") rather than being watched as if it were this server: stop the other one, or give this one another port (D) |

A copy also keeps the original's `data/stats-key.json`, its identity to the stats
service: two copies running at once report as one server.

## Folders Huragok ignores
A folder in `SERVERS/` whose name starts with `.` or `_` is never a server:
Huragok builds new ones as `.new-<file name>` (`.new-infection-night`) and you can
set a server aside by renaming its folder to `_Old Slayer`.
