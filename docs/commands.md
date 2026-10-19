# Commands and who can use them

The server's own admins get kick and ban in game; everything else the server can
do (mute, maps, votes, `say`…) needs its remote console and the RCON password,
which most hosts won't hand out. Vergil, the part of Huragok that watches over
the players, puts all of it in game chat as `!` commands, and `commands.toml` says
who may use each one.

## commands.toml
The first start writes it with every command under its default role, each with
a line on what it does. It looks like this:

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
the vote-kick immune list included. Admins are the server's own list: `!admin add
<player>` (owners) makes someone one in a step, `!admin remove` makes them a
moderator. An admin `dedicated.toml` names stays one until that file says otherwise
(the reply says so). Owners change only in the file.

**A ban takes every role away**, whoever does it (in game, the console, the relay,
automod), and the reply says so ("Alice's moderator role goes with the ban."). Since
nobody acts on their own role or above, only an owner can ban an admin.

Roles follow the **player ID**, never the name: anyone can use any name. A player
ID is 64 letters and digits. Players find theirs in game under Settings >
Online services > Your player ID. `start.cmd rcon players` (Linux:
`./start.sh rcon players`) shows everyone's on the server in full; `!players`
shows the first 8 characters.

Nobody can use a command on someone of their own role or above: a moderator
can't mute an admin, and an admin can't ban another admin. That holds when the
player is named by ID while they're away, too (`!ban <an owner's player ID>` from
an admin reads "… is an owner; you can't do that to them"). Owners can act on anyone.

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
- **The list is in the ban list,** so every server sharing `bans.json` reads the
  same one. `start.cmd rcon immune` shows it: Vergil's entries say `added by
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
- **Naming the player a command acts on** (`!warn`, `!mute`, `!kick`, `!ban`,
  `!strike`, `!strikes`, `!pardonplayer`, `!promote`, `!demote`, `!admin`): their `#number` from
  `!players` (or the number alone), their full player ID, their exact name on the
  server (capitals don't matter), a name Vergil has seen them use, or the first 8
  or more characters of their player ID. The start of a name alone acts on nobody,
  since the one you mean may have left and someone else matches: "Nobody here is
  called 'bobb'. Did you mean Bobby (#6)? Use #6." Names aren't unique, so the
  `#number` is safest.
- A player who has left: `!ban` and `!strike` act on them by their ID (so `!ban
  <a name Vergil has seen>` bans that player while they're away: a decision you
  can ask to have changed); `!kick`, `!mute` and `!warn` say "Bob isn't on this
  server now."; `!unmute` reaches them too, and there the start of a name is enough.
- `!team`, `!startvote kick` and `!whois` still take the start of a name: they
  punish nobody.
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
| `!players` | moderator | Who's on, with `#numbers`, player IDs and roles |
| `!whois <player \| ID>` | moderator | Other names they've used, visits, their last actions |
| `!warn <player> [reason]` | moderator | A private warning, recorded |
| `!mute <player> [time] [reason]` | moderator | Nobody hears their voice or reads their chat |
| `!unmute <player \| ID>` | moderator | Also for someone who left; ends Vergil's own mute on them too |
| `!kick <player> [reason]` | moderator | The server keeps them out 10, 30, 60, then 120 minutes |
| `!pardon <player \| ID \| IP>` | moderator | Let a kicked player back in now |
| `!strike <player> <rule> [note]` | moderator | An automod strike, for something automod can't see, like voice: the rule's next kick, mute or ban |
| `!strike <player> name [note]` | moderator | Bans their name (adds it to `names.txt`): they're kicked every time they join with it, until they change it. No strike, no ban |
| `!health` | moderator | How the server is running |
| `!promote <player>` | moderator | One step up the roles, up to the role below yours |
| `!demote <player>` | moderator | One step down, for someone below you: admin to moderator, moderator to member, member to player |
| `!roles` | moderator | Who has which role |
| `!banname <#number \| name>` | moderator | Ban a name (`names.txt`): a player on by `#number` or name has theirs banned and is kicked; any other text is banned as the name itself. Whoever uses it is kicked as they join, until they change it. No strike, no ban |
| `!ban <player \| ID> [time \| perm] [reason]` | admin | Their ID, address and device; works on players who left, by ID or a name Vergil has seen |
| `!unban <name \| ID \| IP>` | admin | |
| `!pardonplayer <player \| ID> [rule \| all] [reason]` | admin | Clear automod strikes and lift what automod did: unban (only Vergil's own bans), unmute |
| `!unbanname <name>` | admin | Take a name off `names.txt` |
| `!say <text>` | admin | A `[Server]` line for everyone |
| `!map`, `!mode`, `!load`, `!nextmap` | admin | Change the game |
| `!endgame`, `!endround` | admin | |
| `!shuffle`, `!team <player> <color>`, `!teamcount [n]` | admin | Teams |
| `!startvote`, `!cancelvote`, `!passvote` | admin | Votes |
| `!maxping [ms \| off]`, `!servername [name]` | admin | Until the server restarts |
| `!admin [list \| add \| remove]` | owner | The server's own admin list, shared by every server using the ban list: `add` makes a player an admin, `remove` a moderator |
| `!reload` | owner | Apply edits to `dedicated.toml` now |

`!yes` and `!no` are the server's own votes, and lines starting with `/` are the
game's own commands, so neither can be used here.

On a Halo 3: ODST server (Project Reclaimer 0.9.19), which plays only what its file
names, the commands that change the game, teams or rounds (`!map`, `!mode`, `!load`,
`!nextmap <game>`, `!endgame`, `!endround`, `!shuffle`, `!team`, `!teamcount`,
`!startvote` but `!startvote kick`) answer "This server plays Halo 3: ODST: the
console cannot change its game, teams or rounds." without reaching the server.
