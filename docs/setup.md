# Setting servers up

The supervisor makes and changes server folders with the server program's own
`dedicated` commands, so everything it writes is what the Project Reclaimer
program itself accepts. Nothing here needs a server stopped: the servers keep
running while you make a new one, change a playlist or install a Workshop item.

## A new server: N in the window, or `new`

```text
start.cmd new "Infection Night" --package infection
start.cmd new "Team Slayer 2" --package team-slayer --port 49180 --hidden --password letmein
start.cmd new "Rockets" --map Guardian --game rockets --max-players 8
```
(Linux: `./start.sh new …`.) The window's **N** asks the same things: a package, the
name, the port (the next free one is offered), hidden or not, a join password, the
player cap. Then it:

1. makes `SERVERS/<name>/` with `dedicated init` (as `SERVERS/.new-<name>/` until it's
   done, so a half-made folder is never started);
2. points it at the **root's shared folders** (below), or copies Halo 3's files from
   MCC when nothing can be shared or linked;
3. copies the package's playlists and content in, writes the `[[server]]` block (name,
   port, what it plays, your choices), a made-up RCON password, and the `host_name`,
   `stats_server`, `public_address` and `upnp` of the server folder you already have;
4. installs the package's Workshop items with `dedicated mods`;
5. runs `dedicated check`. If it passes, the server appears on the dashboard and starts
   (unless `--no-start`). If not, the folder stays, **autostart off**, and the problems
   are shown: fix them, then **C** (or `dedicated -s "<name>" check`) and **S**.

### What new servers share
By default a new server carries only its own `dedicated.toml`, `playlists/` and
`data/`. The big parts live at the root once, for every server made this way:

| At the root | What | Made when |
| --- | --- | --- |
| `game/` | Halo 3's files (5.5 GB): **hard-linked** from the first server folder that has them (no space used on the same drive), or copied from MCC with `dedicated init --from` | the first new server |
| `content/` | one content folder: maps, game types, mods and images every server can play | the first new server |
| `bans.json` | one ban list, so bans, console-added admins and VPN allowances reach every server at once (what the server program expects: "every server sharing the ban list") | the first new server |

`[setup] share` in `settings.toml` picks which (`["game", "content", "bans"]`;
`--own` on the command line gives a server its own copies). Servers you made by hand
keep their own folders; nothing moves. To put an existing server on the shared
folders, set `game = "../../game"`, `content = "../../content"` and
`bans = "../../bans.json"` in its `dedicated.toml` (relative to that file).

Halo 3's files come from one of: a server folder that has them (linked), your MCC
folder (`[setup] mcc_folder`, or `--from "<folder>"`; `--from` alone means Steam's
default folder; `--maps "Guardian,Valhalla"` copies some maps only), or
`--link-from "<server folder>"`.

The program itself (`project-reclaimer.exe`) is found in any server folder, or **one
copy at the root** serves every server (they must all be on the same version anyway).
With none, `[setup] program` names it.

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

[server]                       # goes into the [[server]] block
playlist = "playlists/infection.playlist.json"
max_players = 16

[defaults]                     # optional: the folder's [defaults]
voice_proximity = false

[workshop]
items = []                     # Workshop item IDs, installed when the server is made
```
Host-specific things stay out (name, port, passwords, admins, `host_name`): the
supervisor asks for them. **Six starter packages** are written to `PACKAGES/` on the
first start (never replaced after that; edit them freely): *Team Slayer*, *Big Team
Battle*, *MLG Hardcore* (Halo 3's own MLG map variants and Hardcore game variants),
*Infection*, *Scripted Modes* (Gun Game, One in the Chamber, Growth, Zone Control,
UNSC vs Covenant) and *Campaign Co-op* (needs the campaign files:
`dedicated init --from "<MCC>" --maps campaign`).

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
     (`1=3 4=2`), and it's saved and checked with `dedicated playlist show`, then
     offered to the server;
   - **edit one**: add games (pick maps and game types), remove by number, weights,
     vote seconds, rename, save and check;
   - **show** one as the server sees it, **use** one for this server (applies from
     the next vote), **open** one in the editor.
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
5. **save it as a package**.
6. **open its folder**.
8. **reload the listing** after changing files by hand.

## SteamCMD for the Workshop: I in the window
A server downloads Workshop items itself, but only with SteamCMD and a Steam account
that owns MCC: an anonymous SteamCMD sign-in can't fetch MCC's items (checked). **I**
(or `start.cmd steam`):

- finds SteamCMD: `[setup] steamcmd`, `steamcmd/` at the root, `C:\steamcmd` (the
  folder the server's own template names), your PATH, `~/steamcmd` on Linux;
- **installs it** when missing, from Valve's installer, into `steamcmd/` at the root
  (kept with the supervisor) or the standard folder, and runs it once so it updates
  itself (`steam install [--standard] [--path]`);
- **signs the account in**: it opens SteamCMD in a window of its own where Steam asks
  for the password and the Steam Guard code **once**; after that the sign-in is cached
  and the servers fetch items without prompts. No password is ever written anywhere
  (`steam login <account>`);
- **tells every server**: `[workshop] steamcmd` and `username` go into each server
  folder's `dedicated.toml` (a server's `[workshop]` takes effect when its `dedicated`
  restarts), and `[setup] steamcmd` / `steam_username` into `settings.toml`;
- can put SteamCMD's folder on your PATH for other tools (`steam path`).

Installing Halo 3's own files through SteamCMD isn't offered: MCC's depots come as one
130 GB app, and `dedicated init --from` already links them from an MCC install for free.
7. **import map and game variants from MCC** (or any folder or file). MCC ships
   every matchmaking variant 343 made for Halo 3 (`halo3/hopper_map_variants`,
   320 `.mvar`: MLG, Team Hardcore, Infection, Grifball…; `hopper_game_variants`,
   334 `.bin`), but a server lists only what's in its content folder. This copies
   them into `content/Maps/From MCC` and `content/Game Modes/From MCC`, optionally
   only the names containing your words (`hardcore`, `mlg`…); the server picks them
   up while running and lists them by name, and playlists can name them. Saved
   `.json` maps and game types from a download go to the folder they belong in.
   Command line: `content -s <server> --import [--from <folder>] [words]`; MCC is
   found by itself when it's installed on this PC.

From the command line: `content [-s server] [words]`, `playlist [-s server] [list |
show <name> | new <name> --maps … --games … | use <name>]`, `workshop [-s server]
[list | add <ids…> | remove <ids…> | install]`, and `dedicated [-s server] <any
dedicated command>` for the rest (`dedicated -s Slayer vpn`, `dedicated -s Slayer
bans`).

## Editing a server's files: E
**E** lists the supervisor's three files, then each server: its `dedicated.toml`,
each playlist, its ban list (or the shared one), `disabled-mods.json`, saved game
types. They open in Notepad (Windows) or the desktop's editor (Linux). Since Project
Reclaimer 0.9.18 the server re-checks `dedicated.toml`, the playlists and the content
folder a couple of seconds after a save (`auto_reload = true`): most settings apply at
once, `friendly_fire`, `team_changing` and a playlist from the next game or vote, ports
and RCON settings by a restart once the server is empty. **The result shows in the
dashboard's events**: `settings reloaded: max_players 16 → 12`, or `settings NOT
reloaded, nothing changed: …` with what's wrong.

## Folders the supervisor ignores
A folder in `SERVERS/` whose name starts with `.` or `_` is never a server: the
supervisor builds new ones as `.new-<name>` and you can set a server aside by
renaming its folder to `_Old Slayer`.
