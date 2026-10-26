# Commands and who can use them

The server's own admins get kick and ban in game; everything else the server can
do (mute, maps, votes, `say`…) needs its remote console and the RCON password,
which most hosts won't hand out. Vergil, the part of Huragok that watches over
the players, puts all of it in game chat as `!` commands, and `commands.toml` says
who may use each one.

## commands.toml
The first start writes it with every command under its default role, each with
a line on what it does. **D → C** in the window changes it for you: owners,
moderators and members picked from the players seen lately, each command moved to
a role or switched off, the prefix and the cooldown. By hand, it looks like this:

```toml
prefix = "!"
cooldown_seconds = 3

[everyone]
commands = ["help", "strikes"]

[member]
players = [
    "0123…cdef",   # Charlie, a regular
]
commands = []

[moderator]
players = [
    "0123…cdef",   # Alice
]
commands = ["players", "whois", "warn", "mute", "unmute", "kick", "pardon", "strike", "health"]

[admin]
commands = ["ban", "unban", "map", "say", …]

[owner]
players = ["…"]
commands = ["admin", "reload"]

[off]
commands = []
```

- **Each role can use its own commands and those of every role before it.**
  Admins can use everything moderators and everyone can, and owners everything.
- **To change who can use a command, move it** to another role's list. To let
  everyone see who's on, move `"players"` to `[everyone]`. To keep `!ban` for
  owners, move `"ban"` to `[owner]`.
- **To switch a command off, put it in `[off]`.** Nobody can use it then, owners
  included, wherever else it's listed, and `!help` stops listing it. `!help`
  itself can't be switched off.
- A command in no list keeps its default role, so commands added in an update
  show up with sensible defaults; `start.cmd check` lists where every command is.
- **Edits apply within a few seconds**, no restart needed. If an edit has a
  mistake (a misspelt command, a command listed twice, something that isn't a
  player ID), the log says what and where, and the file from before stays in force.
  So does a file that's missing or empty for a moment while an editor saves it:
  everyone keeps their role (and their place on the vote-kick immune list), and the
  file is read again once it changes.

## The roles
| Role | Who | Notes |
| --- | --- | --- |
| `everyone` | every player | |
| `member` | the player IDs under `[member]` | Regulars, friends, your clan. No commands of their own yet (list some under `[member]` if you like); no vote can kick them. |
| `moderator` | the player IDs under `[moderator]` | The game itself doesn't know about moderators: what they can do is what's listed for them. No vote can kick them. |
| `admin` | the server's own admins: `admins` in `dedicated.toml`, or added in game with `!admin add` | Not listed in `commands.toml`, so there's never a second list to keep in step. Vergil reads the server's list live. |
| `owner` | the player IDs under `[owner]` | You. Can act on anyone. |

**One role each, on a ladder: player, member, moderator, admin, owner.** In game,
`!promote <player>` moves someone one step up and `!demote <player>` one step
down: demoting an admin makes them a moderator, a moderator a member, a member a
player again. You can promote someone up to the role below your own (moderators
make members, admins make moderators, owners make admins) and demote only someone
below you. `!roles` lists who has which. Moving someone takes them out of the role
they had, so nobody is ever two things. Moderators and members live in
`commands.toml`, each on a line of their own saying who they are and who moved
them (`"…",  # Bravo: promoted by Alice on 2026-10-19`); the rest of the file, your
own comments included, stays as you wrote it, and the change is in force at once,
the vote-kick immune list included. Admins are the servers' own lists: `!admin add
<player>` (owners) makes someone one in a step, on every server (Huragok keeps them
on its own list and puts them on each server's, a new one's too), `!admin remove`
makes them a moderator everywhere. An admin `dedicated.toml` names stays one until
that file says otherwise (the reply says so). Owners change only in the file.

**A ban through Huragok takes every role away** (`!ban`, Vergil's, the relay's, the
window's), on every server, and the reply says so ("Alice's moderator role goes with
the ban."). The game's own `/ban` is that server's alone: the roles stay. Since nobody
acts on their own role or above, only an owner can ban an admin.

Roles follow the **player ID**, never the name: anyone can use any name. A player
ID is 64 letters and digits. Players find theirs in game under Settings >
Online services > Your player ID. `start.cmd rcon players` (Linux:
`./start.sh rcon players`) shows everyone's on the server in full; `!players`
shows the first 8 characters.

Nobody can use a command on someone of their own role or above: a moderator
can't mute an admin, and an admin can't ban another admin. That holds when the
player is named by ID while they're away, too (`!ban <an owner's player ID>` from
an admin reads "… is an owner; you can't do that to them"). Owners can act on anyone.
Admins are each server's own, and a `!` command reaches every server, so a player
counts as the highest role they hold on **any** server: someone who's an admin only
on LG Testing Grounds is an admin everywhere as far as a `!ban` is concerned, and
`!roles` lists them "(on LG Testing Grounds)".

## Votes to kick
Players can vote to kick anyone the server doesn't protect, and the server
protects only its own admins. Vergil protects everyone else you list: every
member, moderator and owner goes on the server's **vote-kick immune list** (the
console's `immune`), as each server connects, within seconds of an edit to
`commands.toml`, and every 5 minutes in case someone took them off. Take a player
out of `commands.toml` and they come off the list again.

- **A vote to kick one of them never opens.** The server refuses it as it's
  called; the caller reads `Charlie can't be vote-kicked on this server.` and,
  from Vergil, `BE A HERO! REPORT VIOLATORS! Charlie is a member here, so no vote
  can kick them. If they're breaking the rules, tell the host.` The dashboard
  shows who tried.
- **The list is in each server's ban list,** and Vergil keeps every server's in
  step. `start.cmd rcon immune` shows one: Vergil's entries say `added by
  Vergil (RCON)`. Vergil only ever takes off entries it added; players you made
  immune yourself (`immune add` on the console, or `vote_kick_immune` in
  `dedicated.toml`) stay.
- **On a server program without the list** (older than the servers' current
  ones), or in the moment before the list is in step, a vote that does get
  called on one of them is cancelled at once, and everyone reads why.
- Moderators and up can still kick, mute and ban members with their commands, as
  their role allows: the protection is from votes only.

## Using commands
- Commands are ordinary chat: **everyone in that chat sees the line you typed.**
  Answers come back privately. That's also why there's no `!password`.
- `!help` lists the commands you can use; `!help mute` explains one.
- **Naming a player.** Every command that takes a player finds them the same way:
  - `#4` (or `4`): the player with that number on this server, from `!players`.
  - `@2`: an entry of the last list Huragok gave you (see below).
  - Their name, as they write it or not (capitals don't matter), **spaces and all,
    no quotes needed**: `!mute nate higgers 30m being rude` mutes Nate Higgers for
    30 minutes. What follows the name is the time, rule or reason, as typed,
    apostrophes too. Quotes work as well, as on the console: `!kick "Nate Higgers"`.
  - Their player ID, or its first 8 characters or more (lists show the first 8).
- **When a name could be more than one player, nobody is acted on.** You get each of
  them with what tells them apart, and what to type:

  ```text
  'Bob' could be #6 Bob (red, on 12m, 1a1a1a1a); @1 bob (seen 2h ago, 2b2b2b2b);
  @2 BOB (seen 3d ago, 3c3c3c3c). Use the #number or @number.
  ```

  `#6` is someone on this server: their team, how long they've been on, their role
  if they have one, "muted", the start of their player ID. `@1` and `@2` are players
  who aren't on (for the commands that reach them, like `!ban`): when they were last
  here, or the server they're on now. `!ban @2 7d cheating` bans exactly that one,
  by player ID, never whoever has the name now. Your @numbers are yours alone, and
  last 10 minutes or until your next list.
- The same holds for a name that's the start of a longer one: with Nate and Nate
  Higgers both on, `!kick nate higgins spam` (a typo) asks rather than kicking Nate,
  unless the next word settles it (a time in `!mute nate 10m`, a rule in `!strike`).
- **The start of a name acts on nobody**: "Nobody here is called 'bobb'. Did you
  mean Bobby (#6)? Use #6." `!whois`, `!unmute`, `!said` and `!team`, which punish
  nobody, take a name's start when only one player on has it.
- **A name that only looks like it acts on nobody either.** Letters of another
  script or style (a Greek `Β`, small capitals like `ʙʀᴀᴠᴏ`, fullwidth or styled
  letters) look like plain ones: `!kick bravo` for a player who wrote `ΒRAVO` says
  "Nobody here is called 'bravo'. This looks like it: #6 ΒRAVO (…). Use the
  #number." And with the real Bravo on beside one like that, `!kick bravo` lists
  both, as for two players with one name, so an impersonator never gets the real
  player kicked in their place.
- **A number at the end of `!whois`** is how many lines, unless it's part of a
  name: with "Player 2" on, `!whois player 2` is him; with "Player" on too, it
  asks which you meant (`!whois #6`, or `!whois #7 2` for Player's latest two).
- **Lists to pick from.** `!bans` (admins), `!kicks` and `!mutes` (moderators) and
  `!bannames` number their entries @1, @2…, so `!unban @2`, `!pardon @1`, `!unmute
  @3` and `!unbanname @1` lift exactly the one you picked. `!unban`, `!pardon` given a
  name look in the same list, and ask with @numbers when it's on more than one entry.
- **Who a command reaches.** Each server's moderation commands are its own: `!warn`,
  `!kick` and `!team` act on players on this server ("Zed is on Other Server, not
  this server."); `!mute` too, and also someone who left (they're muted as they next
  join, by player ID, never someone who takes the name meanwhile). `!ban`,
  `!strike`, `!pardonplayer`, `!unmute`, `!whois` and `!said` reach players who left.
  What a mute, kick or ban does then reaches every server (below).
- **A mute lasts its time.** The server forgets every mute when it restarts (a crash,
  the daily restart, the watchdog's), so Vergil keeps each one on its list and mutes
  the player again as they join until it's up: a `!mute`, an admin's own mute in
  game, one from the relay or the console. A mute with no time lasts until `!unmute`.
  A newer mute replaces the older one, and `!mutes` lists them. This needs automod
  switched on (in any mode).
- **`!` reaches every server, `/` stays on one** (`[automod] across_servers`, on by
  default; changed in the window, D → S → Vergil). What Huragok does goes across: a
  `!mute` (or Vergil's) follows the player to any server they join; a `!kick` keeps
  them off every server until its time is up ("Kicked from LG Test Server by Alice
  (spam): you can join again in 7m."); a `!ban` reaches every server, and so does an
  `!unban`. The game's own `/kick` and `/ban`, and votes, stay on the server they
  were used on, and Huragok only writes them on the player's record.
- **Every server keeps its own ban list; Huragok keeps one of its own** of the bans
  it gives (`!ban`, Vergil, the relay, the window's console) and the admins it
  makes, and puts them on every server's: as they're given, whenever a server's
  console connects, every 10 minutes, and on a player who joins a server that
  doesn't have their ban yet. `!bans` says of each ban "every server" or "this
  server"; **`!banall @number`** (admins) puts one of this server's (a `/ban`) on
  Huragok's list, and so on every server's, with its time left, reason and who gave
  it. The window does the same: B → the server → E.
- **`!whois <player> [lines]` is their whole record**: where they are, their other
  names, what's on them now, totals by kind and by how (`!`, `/`, Vergil, a vote,
  the console), strikes on each line, and the latest entries with who, why, how long
  and on which server.
- Times are like `30m`, `1h`, `7d`, `2w`; `perm` means for good.
- `cooldown_seconds` holds back repeats of the same command by the same player.
  Admins and owners aren't held back.
- Every warn, mute, kick and ban is recorded with who did it and why, and players
  read who acted ("Bravo was muted by Alice").
- A reply a slow or restarting server can't take in time is dropped (the log says
  so); the commands keep working.

## Every command
| Command | Default role | |
| --- | --- | --- |
| `!help [command]` | everyone | The commands you can use, or how to use one |
| `!strikes [player]` | everyone | Your automod strikes; moderators can name anyone |
| `!players [name]` | moderator | Who's on, with `#numbers`, player IDs, team, time on and roles; `!players nate` lists only those with "nate" in their name (a busy server has more than one answer shows) |
| `!whois <player \| ID> [lines]` | moderator | Their whole record, from every server: where they are, other names, what's on them now (a mute, a kick keeping them out, their bans as each server's list has them), every kick, mute, ban and warning (by `!`, `/`, Vergil, a vote or the console, who, why, on which server), strikes on each line |
| `!warn <player> [reason]` | moderator | A private warning, recorded |
| `!mute <player> [time] [reason]` | moderator | Nobody hears their voice or reads their chat; someone who left is muted as they next join |
| `!unmute <player \| ID>` | moderator | Also for someone who left; ends Vergil's own mute on them too. Someone on another server is unmuted there |
| `!kick <player> [reason]` | moderator | The server keeps them out 10, 30, 60, then 120 minutes |
| `!pardon <@number \| name \| ID \| IP>` | moderator | Let a kicked player back in now; a `!kick`'s keeps them off every server, so it's pardoned where it was given and they can join every server again; `@number` from `!kicks` |
| `!kicks` | moderator | Who's been kicked here lately (two hours, since Huragok started), and who a `!kick` on another server keeps off this one, with who, why and how long they're out; numbered for `!pardon` |
| `!mutes` | moderator | Who's muted: Vergil's list (strikes', moderators', admins') and the server's own; numbered for `!unmute` |
| `!said <player> [lines]` | moderator | The last lines (5, up to 15) they typed in chat, on any server; kept 30 days |
| `!rules [line \| word]` | moderator | The word list: each line's name, words and what its strikes do; one line in full |
| `!bannames [text]` | moderator | The banned names (`names.txt`), numbered for `!unbanname` |
| `!strike <player> <rule> [note]` | moderator | An automod strike, for something automod can't see, like voice: the rule's next kick, mute or ban |
| `!strike <player> name [note]` | moderator | Bans their name (adds it to `names.txt`): they're kicked every time they join with it, until they change it. No strike, no ban |
| `!health` | moderator | How the server is running |
| `!promote <player>` | moderator | One step up the roles, up to the role below yours |
| `!demote <player>` | moderator | One step down, for someone below you: admin to moderator, moderator to member, member to player |
| `!roles` | moderator | Who has which role |
| `!ban <player \| ID> [time \| perm] [reason]` | admin | Their ID, address and device; works on players who left, by ID or a name Vergil has seen |
| `!unban <@number \| name \| ID \| IP>` | admin | Lift a ban with everything linked to it, on every server; another server's own `/ban` of them stays, and the reply says where; `@number` from `!bans` |
| `!bans [name]` | admin | The bans in force, newest first, with who banned, why, how long is left, and whether it's on every server or this one; numbered for `!unban` and `!banall` |
| `!banall <@number \| player \| ID>` | admin | Put a ban from this server's list (a `/ban`) on every server's: on Huragok's list, with its time left, reason and who gave it; `@number` from `!bans` |
| `!changebanword <line \| word> \| kicks \| mutes \| ban` | admin | New steps for a line of the word list, its words and name kept: `!changebanword slurs \| 1h 1d \| 2, 1d 7d \| 5` |
| `!banword <words> <line>` | admin | Add words to a line of the word list (`automod.txt`), with its steps: `!banword word slurs` (`slur` finds `slurs`). A line that isn't there: the answer says how to make it |
| `!banword <words> \| [kicks] \| [mutes] \| [ban] \| <name>` | admin | Make a new line: `!banword word \| 1h 1d \| 2, 1d 7d \| 5 \| slurs`. Applies at once, in chat and to names with the word in them. See [automod.md](automod.md#changing-the-word-list-from-the-game) |
| `!unbanword <word>` | admin | Take a word off `automod.txt` (a line left with no words goes too) |
| `!banname <#number \| name>` | admin | Ban a name (`names.txt`): a player on by `#number` or name has theirs banned and is kicked; any other text is banned as the name itself. Whoever uses it is kicked as they join, until they change it. No strike, no ban. A moderator's up to 0.4.4 |
| `!pardonplayer <player \| ID> [rule \| all] [reason]` | admin | Clear automod strikes and lift what automod did: unban (only Vergil's own bans), unmute |
| `!unbanname <name>` | admin | Take a name off `names.txt` |
| `!say <text>` | admin | A `[Server]` line for everyone |
| `!map`, `!mode`, `!load`, `!nextmap` | admin | Change the game |
| `!endgame`, `!endround` | admin | |
| `!shuffle`, `!team <player> <color>`, `!teamcount [n]` | admin | Teams |
| `!startvote`, `!cancelvote`, `!passvote` | admin | Votes |
| `!maxping [ms \| off]`, `!servername [name]` | admin | Until the server restarts |
| `!restartserver [reason]` | admin | Restart this server, as the window's R does when players are on: everyone reads the usual countdown (`[restarts] warn_seconds`, 5 minutes by default) with the reason and who asked ("Server restarting in 5 minutes: lag (requested by Alice)."), and it goes at once if everyone leaves. A countdown already under way (the daily restart's, a stop) keeps its time and ends in a restart. Only the server it's typed on. For a server running poorly that needs a fresh start |
| `!toggleglobal [on \| off] [time]` | admin | Global voice (the V key: everyone hears) on or off, on this server only. Alone it switches it over and it stays; `on` or `off` sets it. With a time (`30s`, `10m`, `2h`, `1d`) it switches back once that's up, either way: `!toggleglobal 10m` turns it off for 10 minutes, or on for 10 minutes if it was off. Written to the server's own block in `dedicated.toml`, which the server applies within seconds: no restart, nobody dropped |
| `!toggleteam [on \| off] [time]` | admin | Team voice (the B key: teammates hear), the same way |
| `!toggleproxy [on \| off] [time]` | admin | Proximity voice (the N key: players nearby hear), the same way |
| `!togglevoice` | admin | All voice chat off on this server for 10 minutes, then back on by itself; typed again, it's back on at once. Always 10 minutes: it's for stopping voice spam, so it can't be left off |
| `!admin [list \| add \| remove]` | owner | The server's own admin list, shared by every server using the ban list: `add` makes a player an admin, `remove` a moderator |
| `!reload` | owner | Apply edits to `dedicated.toml` now |

Everyone on the server reads each toggle as a `[Server]` line ("Alice turned global voice (V) off for 10 minutes."), and again when a timed one switches back ("Global voice (V) is back on after 10 minutes."). A timed toggle is kept by Huragok through its own restarts; one whose time ran out while Huragok was down switches back as it starts. Someone changing the setting in the meantime (another toggle, or the window's D) wins: it isn't switched back. There's no `!togglechat`: the server's own lines are text chat, so with it off nobody could read why, or type the command to bring it back.

`!yes` and `!no` are the server's own votes, and lines starting with `/` are the
game's own commands, so neither can be used here.

On a Halo 3: ODST server (Project Reclaimer 0.9.19), which plays only what its file
names, the commands that change the game, teams or rounds (`!map`, `!mode`, `!load`,
`!nextmap <game>`, `!endgame`, `!endround`, `!shuffle`, `!team`, `!teamcount`,
`!startvote` but `!startvote kick`) answer "This server plays Halo 3: ODST: the
console cannot change its game, teams or rounds." without reaching the server.
