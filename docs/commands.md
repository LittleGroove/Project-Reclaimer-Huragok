# Commands and who can use them

The server's own admins get kick and ban in game; everything else the server can
do (mute, maps, votes, `say`…) needs its remote console and the RCON password,
which most hosts won't hand out. The supervisor puts all of it in game chat as
`!` commands, and `commands.toml` says who may use each one.

## commands.toml
The first start writes it with every command under its default role, each with
a line on what it does. It looks like this:

```toml
prefix = "!"
cooldown_seconds = 3

[everyone]
commands = ["help", "strikes"]

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

## The roles
| Role | Who | Notes |
| --- | --- | --- |
| `everyone` | every player | |
| `moderator` | the player IDs under `[moderator]` | The game itself doesn't know about moderators: what they can do is what's listed for them. They can be vote-kicked. |
| `admin` | the server's own admins: `admins` in `dedicated.toml`, or added in game with `!admin add` | Not listed in `commands.toml`, so there's never a second list to keep in step. The supervisor reads the server's list live. |
| `owner` | the player IDs under `[owner]` | You. Can act on anyone. |

Roles follow the **player ID**, never the name: anyone can use any name. A player
ID is 64 letters and digits. Players find theirs in game under Settings >
Online services > Your player ID. `start.cmd rcon players` (Linux:
`./start.sh rcon players`) shows everyone's on the server in full; `!players`
shows the first 8 characters.

Nobody can use a command on someone of their own role or above: a moderator
can't mute an admin, and an admin can't ban another admin. Owners can act on anyone.

## Using commands
- Commands are ordinary chat: **everyone in that chat sees the line you typed.**
  Answers come back privately. That's also why there's no `!password`.
- `!help` lists the commands you can use; `!help mute` explains one.
- Name a player by their `#number` from `!players`, their player ID, or their
  name (or its start). Names aren't unique, so the `#number` is safest.
- Times are like `30m`, `1h`, `7d`, `2w`; `perm` means for good.
- `cooldown_seconds` holds back repeats of the same command by the same player.
  Admins and owners aren't held back.
- Every warn, mute, kick and ban is recorded with who did it and why, and players
  read who acted ("Bravo was muted by Alice").

## Every command
| Command | Default role | |
| --- | --- | --- |
| `!help [command]` | everyone | The commands you can use, or how to use one |
| `!strikes [player]` | everyone | Your automod strikes; moderators can name anyone |
| `!players` | moderator | Who's on, with `#numbers`, player IDs and roles |
| `!whois <player \| ID>` | moderator | Other names they've used, visits, their last actions |
| `!warn <player> [reason]` | moderator | A private warning, recorded |
| `!mute <player> [time] [reason]` | moderator | Nobody hears their voice or reads their chat |
| `!unmute <player>` | moderator | |
| `!kick <player> [reason]` | moderator | The server keeps them out 10, 30, 60, then 120 minutes |
| `!pardon <player \| ID \| IP>` | moderator | Let a kicked player back in now |
| `!strike <player> <rule> [note]` | moderator | An automod strike, for something automod can't see, like voice |
| `!health` | moderator | How the server is running |
| `!ban <player \| ID> [time \| perm] [reason]` | admin | Their ID, address and device; works on players who left, by ID |
| `!unban <name \| ID \| IP>` | admin | |
| `!pardonplayer <player \| ID> [rule \| all] [reason]` | admin | Clear automod strikes and lift what automod did |
| `!say <text>` | admin | A `[Server]` line for everyone |
| `!map`, `!mode`, `!load`, `!nextmap` | admin | Change the game |
| `!endgame`, `!endround` | admin | |
| `!shuffle`, `!team <player> <color>`, `!teamcount [n]` | admin | Teams |
| `!startvote`, `!cancelvote`, `!passvote` | admin | Votes |
| `!maxping [ms \| off]`, `!servername [name]` | admin | Until the server restarts |
| `!admin [list \| add \| remove]` | owner | The server's own admin list, shared by every server using the ban list |
| `!reload` | owner | Apply edits to `dedicated.toml` now |

`!yes` and `!no` are the server's own votes, and lines starting with `/` are the
game's own commands, so neither can be used here.
