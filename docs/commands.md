# Commands and who can use them

The server's own admins get kick and ban in game; everything else the server can
do (mute, maps, votes, `say`…) needs its remote console and the RCON password,
which most hosts won't hand out. Vergil, the part of Huragok that watches over
the players, puts all of it in game chat as `!` commands, and `commands.toml` says
who may use each one.

## commands.toml
The first start writes it with every command under its default role, each with
a line on what it does. **D → C** in the window changes it for you: faculty,
moderators and members picked from the players seen lately, each command moved to
a role or switched off, the prefix and the cooldown. By hand, it looks like this:

```toml
prefix = "!"
cooldown_seconds = 3

[everyone]
commands = ["help", "strikes", "respawn", "calladmin", "report", "mods", "link", "stats", "season"]

[member]
players = [
    "0123…cdef",   # Charlie, a regular
]
commands = ["side", "togglesides"]

[moderator]
players = [
    "0123…cdef",   # Alice
]
commands = ["players", "whois", "warn", "mute", "unmute", "kick", "pardon", "strike", "health"]

[admin]
commands = ["ban", "unban", "map", "say", …]

[faculty]
players = ["…"]
commands = ["admin", "reload", "newseason", "endseason"]

[off]
commands = []
```

- **Each role can use its own commands and those of every role before it.**
  Admins can use everything moderators and everyone can, and faculty everything.
- **To change who can use a command, move it** to another role's list. To let
  everyone see who's on, move `"players"` to `[everyone]`. To keep `!ban` for
  faculty, move `"ban"` to `[faculty]`.
- **To switch a command off, put it in `[off]`.** Nobody can use it then, faculty
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
| `probationary-moderator` | members on probation as moderators: made one in game with `!promote` | Moderators' commands, but each kick, mute, strike, warning… is a request a moderator or admin confirms before it happens: see [Probationary moderators](#probationary-moderators). Listed under `[member]`, with a note saying so. No vote can kick them. |
| `moderator` | the player IDs under `[moderator]` | The game itself doesn't know about moderators: what they can do is what's listed for them. No vote can kick them. |
| `admin` | the server's own admins: `admins` in `dedicated.toml`, or added in game with `!admin add` | Not listed in `commands.toml`, so there's never a second list to keep in step. Vergil reads the server's list live. |
| `faculty` | the player IDs under `[faculty]` | You. Can act on anyone. |

**One role each, on a ladder: player, member, probationary moderator, moderator, admin,
faculty.** In game, `!promote <player>` moves someone one step up and `!demote <player>`
one step down: demoting an admin makes them a moderator, a moderator a probationary
moderator, then a member, a member a player again. You can promote someone up to the
role below your own (moderators make members and probationary moderators, admins make
moderators, faculty make admins) and demote only someone below you. `!roles` lists who has which. Moving someone takes them out of the role
they had, so nobody is ever two things. Moderators and members live in
`commands.toml`, each on a line of their own saying who they are and who moved
them (`"…",  # Bravo: promoted by Alice on 2026-10-19`); the rest of the file, your
own comments included, stays as you wrote it, and the change is in force at once,
the vote-kick immune list included. Admins are the servers' own lists: `!admin add
<player>` (faculty) makes someone one in a step, on every server (Huragok keeps them
on its own list and puts them on each server's, a new one's too), `!admin remove`
makes them a moderator everywhere. An admin `dedicated.toml` names stays one until
that file says otherwise (the reply says so). Faculty change only in the file.

**A ban for good takes every role away**, whoever gave it (`!ban`, Vergil's, the relay's,
the window's, Discord's, the game's own `/ban`), on every server, and `!ban`'s reply says so
("Alice's moderator role goes with the ban."). A kick with a time takes nothing away:
they'll be back, and so will their role. With `[automod] across_servers` off, the game's
own `/ban` is that server's alone, and the roles stay. Since nobody acts on their own role
or above, only faculty can ban an admin: an admin's own `/ban` of a fellow admin or of
faculty stays on that server, and takes no role.

Roles follow the **player ID**, never the name: anyone can use any name. A player
ID is 64 letters and digits. Players find theirs in game under Settings >
Online services > Your player ID. `start.cmd rcon players` (Linux:
`./start.sh rcon players`) shows everyone's on the server in full; `!players`
shows the first 8 characters.

Nobody can use a command on someone of their own role or above: a moderator
can't mute an admin, and an admin can't ban another admin. That holds when the
player is named by ID while they're away, too (`!ban <faculty's player ID>` from
an admin reads "… is faculty; you can't do that to them"). Faculty can act on anyone.
**A rank on one server is a rank on every server** (`[automod] across_servers`, on by
default): someone on Server 2's admin list is an admin everywhere, for what they can do
and for what can be done to them; `!roles` lists them "(on Server 2)", and `!admin list`
"(on Server 2's list)".
With it off, what someone can do is their rank on the server they're typing on, and
what protects them is still the highest role they hold on **any** server.

## Votes to kick
Players can vote to kick anyone the server doesn't protect, and the server
protects only its own admins. Vergil protects everyone else you list: every
member, moderator and faculty goes on the server's **vote-kick immune list** (the
console's `immune`), as each server connects, within seconds of an edit to
`commands.toml`, and every 5 minutes in case someone took them off. Take a player
out of `commands.toml` and they come off the list again.

- **A vote to kick one of them never opens.** The server refuses it as it's
  called; the caller reads `Charlie can't be vote-kicked on this server.` and,
  from Vergil, `[^_^] BE A HERO! REPORT VIOLATORS! Charlie is a member here, so no vote
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

## Players who spam votes
`!novote Bravo 10m spamming votes` takes Bravo's votes away: any vote they call (to
kick, end the round or the game, shuffle) is cancelled the moment it opens, and they
read `[-_-] OBEY POSTED LIMITS! Your vote was cancelled: Mod took your votes away for
another 9 minutes (spamming votes). You can still vote on other players' votes.`
Everyone reads `[-_-] OBEY POSTED LIMITS! Mod took Bravo's votes away for 10 minutes:
spamming votes. Any vote they call is cancelled.` when it's given.

- **Only calling votes is taken away.** They still answer everyone else's votes
  (F5, F6, `!yes`, `!no`).
- **Any time works** (`30s`, `10m`, `2h`, `1d`, `2w`). With no time (or `perm`) it
  lasts until a moderator gives their votes back with `!allowvote Bravo`. `!novote`
  again with a new time replaces the old one.
- **It follows the player ID,** so leaving, rejoining or changing name keeps it, and
  it's kept through Huragok's restarts. Someone who just left can be picked the way
  `!mute` picks them (`@number`); it applies from when they're back.
- **Every server,** like Huragok's other moderation, or only the server it was given
  on with `[automod] across_servers = false`.
- `!novotes` lists whose votes are away, numbered for `!allowvote @number`; `!whois`
  shows it under "Now".
- The server has no setting of its own to stop one player calling votes, so Vergil
  cancels each one as it opens: players see the server's own cancel line for a moment.
  When two players have the caller's name, the vote is left alone: it could be the
  other one's.

## Using commands
- Commands are ordinary chat: **everyone in that chat sees the line you typed.**
  Answers come back privately. That's also why there's no `!password`.
- `!help` lists the commands you can use; `!help mute` explains one.
- **How a player is written.** Everywhere Huragok names a player (a reply, a list, a
  confirmation, Discord, the window, the log) it's the same:

  ```text
  Nate Higgers 1a2b3c4d (#4) [HugeGrass]
  ```

  Their name, the first 8 characters of their player ID, the `#number` or `@number`
  that picks them, and the server they're on (left out when they're on none). Read it
  from the right: the server in square brackets, the number in round ones, then 8
  letters and digits; what's left is the name, whatever it has in it. A confirmation
  says back the `#` or `@` you typed: `!mute #4 30m spam` answers "Nate Higgers
  1a2b3c4d (#4) [HugeGrass] is muted on every server for 30 minutes."
- **Naming a player.** Every command that takes a player finds them the same way:
  - `#4` (or `4`): the player with that number on this server, from `!players`.
  - `@2`: one of your @numbers (see below).
  - Their name, as they write it or not (capitals don't matter), **spaces and all,
    no quotes needed**: `!mute nate higgers 30m being rude` mutes Nate Higgers for
    30 minutes. What follows the name is the time, rule or reason, as typed,
    apostrophes too. Quotes work as well, as on the console: `!kick "Nate Higgers"`.
  - Their player ID, or its first 8 characters or more, as every reply shows them:
    `!kick 1a2b3c4d`. It works on every server and never runs out.
- **When a name could be more than one player, nobody is acted on.** You get each of
  them, one a line, with what tells them apart, and what to type:

  ```text
  'Bob' could be 3 players. Use the #number or @number:
  Bob 1a1a1a1a (#6) [HugeGrass]: red, on 12m
  bob 2b2b2b2b (@1) [Minigames]
  BOB 3c3c3c3c (@2): seen 3d ago
  ```

  `#6` is someone on this server: their team, how long they've been on, their role
  if they have one, "muted". `@1` and `@2` are players who aren't on this server
  (for the commands that reach them, like `!ban`): the server they're on, or when they
  were last here. `!ban @2 cheating` bans exactly that one, by player ID, never
  whoever has the name now.
- **Your @numbers.** Anyone a reply names who isn't on your server gets one, so you can
  always act on whoever you're shown. They stick: someone keeps their @number in every
  list and reply that names them, and a new list never renumbers anyone, so `@2` never
  quietly becomes someone else. They're yours alone, and run out 10 minutes after you
  last used or saw one. `!players <server>` lists another server's players, and
  `!players all` everyone's, each with an @number.
- The same holds for a name that's the start of a longer one: with Nate and Nate
  Higgers both on, `!kick nate higgins spam` (a typo) asks rather than kicking Nate,
  unless the next word settles it (a time in `!mute nate 10m`, a rule in `!strike`).
- **The start of a name acts on nobody**: "Nobody here is called 'bobb'. Did you
  mean this? Use #6:" and Bobby's line. `!whois`, `!unmute`, `!said` and `!team`, which punish
  nobody, take a name's start when only one player on has it.
- **A name that only looks like it acts on nobody either.** Letters of another
  script or style (a Greek `Β`, small capitals like `ʙʀᴀᴠᴏ`, fullwidth or styled
  letters) look like plain ones: `!kick bravo` for a player who wrote `ΒRAVO` says
  "Nobody here is called 'bravo'. This looks like it; use the #number:" and the
  player's line. And with the real Bravo on beside one like that, `!kick bravo` lists
  both, as for two players with one name, so an impersonator never gets the real
  player kicked in their place.
- **A number at the end of `!whois`** is how many lines, unless it's part of a
  name: with "Player 2" on, `!whois player 2` is him; with "Player" on too, it
  asks which you meant (`!whois #6`, or `!whois #7 2` for Player's latest two).
- **Lists to pick from.** `!bans` (admins), `!kicks` and `!mutes` (moderators) and
  `!bannames` number their entries @1, @2…, so `!unban @2`, `!pardon @1`, `!unmute
  @3` and `!unbanname @1` lift exactly the one you picked. `!unban`, `!pardon` given a
  name look in the same list, and ask with @numbers when it's on more than one entry.
- **Lifting takes the rank of whoever gave it.** A moderator's `!unmute`, `!allowvote`
  or `!pardon` of what an admin did is refused, and says who: "Not unmuted: Alice gave
  that mute, and lifting it takes an admin or above." So is a moderator's `!novote` over
  an admin's (a new time lifts the old one). Vergil's own, a vote's and the server's
  have no rank: any moderator lifts them.
- **Who a command reaches.** `!warn` and `!team` act on players on this server ("Zed is
  on Other Server, not this server."). `!mute`, `!unmute` and `!kick` act wherever the
  player is: on another server, they're sent there. Someone who left is muted as they
  next join (by player ID, never someone who takes the name meanwhile); a kick for
  someone on no server needs a time, and their player ID or an `@number` from a list,
  and keeps them off every server as they join. `!ban`, `!strike`, `!pardonplayer`,
  `!unmute`, `!whois` and `!said` reach players who left. With `[automod]
  across_servers` off, `!mute`, `!unmute` and `!kick` act only on players on this
  server ("… not this server: with [automod] across_servers off, each server's
  moderation is its own.").
- **A mute lasts its time.** The server forgets every mute when it restarts (a crash,
  the daily restart, the watchdog's), so Vergil keeps each one on its list and mutes
  the player again as they join until it's up: a `!mute`, an admin's own mute in
  game, one from the relay or the console. A mute with no time lasts until `!unmute`.
  A newer mute replaces the older one, and `!mutes` lists them. This needs automod
  switched on (in any mode).
- **Moderation reaches every server, whoever gives it** (`[automod] across_servers`, on
  by default; changed in the window, D → S → Vergil). A mute follows the player to any
  server they join; a kick keeps them off every server until its time is up ("Kicked
  off every server by Alice (on Server 1): spam. You can join any server again in 7
  minutes."); a ban reaches every server, and so does an unban or letting someone back.
  That's so for a `!` command, Vergil, Discord, the console, the relay, the window, and
  the game's own `/kick`, `/ban` and scoreboard. Only a vote's kick and the server's own
  stay on the server they happened on, as do the server's own settings and commands
  (the toggles, `!team`, `!respawn`, restarts). An admin's own `/kick` or `/ban` of a
  fellow admin or of faculty stays on its server too, as `!ban` would refuse it. Players
  read how far: a strike's "muted for 1 day on every server", a `!kick` with a time's
  "Kicked off every server for 7 days: …", and the reason for any other `!mute`, `!kick`
  or `!ban` ending "(every server)". With it off, each server's moderation is its own.
- **Every server keeps its own ban list; Huragok keeps one of its own** of every ban
  given and the admins made, and puts them on every server's: as they're given,
  whenever a server's console connects, every 10 minutes, and on a player who joins a
  server that doesn't have their ban yet. At those looks, a ban on a server's own list
  that Huragok's doesn't have (a `/ban` from before 0.7.0, or one given while Huragok
  was down) goes on Huragok's, and so on every server's. `!bans` says of each ban
  "every server" or "this server"; **`!banall @number`** (admins) puts one of this
  server's on Huragok's list at once, rather than at the next look, with its time left,
  reason and who gave it. The window does the same: B → the server → E.
- **`!whois <player> [lines]` is their whole record**: where they are, their other
  names, what's on them now, totals by kind and by how (`!`, `/`, Vergil, a vote,
  the console), strikes on each line, and the latest entries with their case numbers,
  who, why, how long and how far: "every server" for a mute, kick or ban someone gave,
  the server's name for a vote's or the server's own, nothing for a warning. A kick
  with a time is a kick there.
- Times are like `30m`, `1h`, `7d`, `2w`; `perm` means for good.
- `cooldown_seconds` holds back repeats of the same command by the same player.
  Admins and faculty aren't held back.
- Every warn, mute, kick and ban is recorded with who did it and why, and players
  read who acted ("Bravo was muted by Alice").
- A reply a slow or restarting server can't take in time is dropped (the log says
  so); the commands keep working.

## Probationary moderators
A way to bring new moderators in. A moderator
promotes a member once more and they're a **probationary moderator**; an admin
promotes them to moderator once they've shown they're ready.

- **They use moderators' commands, as moderators type them.** The looking ones work
  as they do for a moderator (`!players`, `!whois`, `!said`, `!kicks`, `!mutes`,
  `!rules`, `!roles`, `!health`, `!bannames`, `!novotes`, `!requests`, `!reports`). The others,
  which name a player (`!kick`, `!mute`, `!strike`, `!warn`, `!unmute`, `!pardon`,
  `!novote`, `!allowvote`, `!promote`, `!demote`), become **requests**: the player is
  found with the usual checks, and then nothing happens yet. They read
  `Sent for review as #3: !kick Bravo spam.`; every moderator and admin on any
  server reads, privately, `[o_o] BE A HERO! REPORT VIOLATORS! Nate (probationary
  moderator) asks, #3 on Server 1: !kick Bravo spam. !confirm 3 or !deny 3` (one asked
  from Discord reads "#3 from Discord"); the player named reads nothing.
- **`!requests`** lists the requests waiting, on every server. **`!confirm 3`** makes
  it happen, as the confirmer's own (they're who acts: the player reads their name),
  on the server the player is on now. **`!confirm 3 strike slurs`**, or `!confirm 3
  mute 1h language`, does that to the same player instead. **`!deny 3 [why]`** turns
  it down. A request's number is the lowest one free; a decided one frees it. With the
  Discord bot, each is a card too, whose Confirm and Deny do the same.
- **Five at most.** Each probationary moderator has 5 requests waiting at most: a sixth
  is refused until one is decided. And theirs are dropped once they aren't on probation
  any more (demoted, banned for good, taken out of `commands.toml`).
- **Nothing runs out.** A request waits until someone decides it, and what's
  confirmed happens then, its time counting from that moment, even for a player who
  has left: a mute as they next join; a kick keeps them off every server for 10
  minutes from the confirmation, or for its time if it was given one, within what
  the confirmer's rank can give (`perm` is a ban, and needs that rank); a warning waits
  for their next join; a strike counts at once.
- **The probationary moderator is told how each went**, now or as they next join, or
  by DM if they asked from Discord.
  `!whois <them>` shows their record ("Requests as a probationary moderator: 12 (9
  confirmed, 2 turned down, 1 waiting)"), and the ledger keeps "requested by" beside
  the confirmer's action.
- They can't confirm requests, and nobody confirms their own.

## Telling the staff
`!calladmin`, `!report` and `!mods` are for hosts with the Discord bot
([discord.md](discord.md)). They only exist while Discord can take them: without it, or
with that part of it switched off, they read "Unknown command" and `!help` doesn't list
them. `[off]` in `commands.toml` switches them off either way.

- **`!calladmin [why]`** puts a card in front of the staff, with the last of the
  server's chat on it. Once per `cooldown_seconds` in `[feed.calls]`. The `words` there
  are chat words that call too.
- **`!report <player> [why]`** tells them about a player: someone on, or someone who's
  left, by a name Vergil has seen. Not yourself. A player has one card while it's open,
  however many report them, and saying it again adds to yours. The player isn't told who
  reported them. Once in `reporter_cooldown_seconds`, and `most_at_once` open at a time.
- **Moderators and up, on any server, are told privately** as each comes in, since
  they're the only staff who can hear what's being reported: `[o_o] BE A HERO! REPORT
  VIOLATORS! Charlie reported Bravo on Server 1: voice abuse` (`tell_in_game` in
  `[reports]`, in `discord.toml`). **`!reports`** lists what's open, on every server.
- **Whoever reported is told when staff act on it** ("The staff acted on your report of
  Bravo."), now or as they next join (`tell_reporter`).
- **Quoting a word doesn't count.** A word from the word list in `!report` or
  `!calladmin` isn't struck, while Discord can take them: whoever's reporting it is
  helping. See [automod.md](automod.md#what-players-read).
- **`!mods`** says which staff are in a game now. Staff who are only on Discord aren't
  counted: `!calladmin` reaches them.

## Stats
**`!stats [player]`** (0.6.1) is anyone's record on these servers, from every game the servers reported to the
stats service: `Alpha: 32 games, 21 won (66%), K/D 2.26, best streak 27, most kills with the tank. Rank: Apprentice.`
Left out, it's your own. It's there with or without Discord; with the bot, `/stats` and `/leaderboard` say more.

## Seasons and sides
**`!season`** says how Red and Blue stand in the season running now (between seasons, the one just ended): the team
games each colour won, in Red against Blue games only (not Infection or Juggernaut, nor a game with a third team);
**`!season 3`** says a past one's. Faculty
start one with **`!newseason [name] [length]`** (`!newseason Winter 30d`): with no length it runs until faculty
type **`!endseason`**. With the Discord bot's `[sides]` on, members and up pick their team with **`!side red`**,
`!side blue` or `!side off`, and are put on it in the lobby, or during a round as soon as that can't end the game
(never a team's last player), the teams within 2. **`!togglesides`** stops it, or starts it again, for whoever types
it, except while `force_for_season` holds their side for the season. Someone who switches team themselves isn't
moved again that game.

## Linking to Discord
**`!link`** (0.6.1) is for hosts with the Discord bot and `[link] enabled` in `discord.toml`; without it, it reads
"Unknown command". It tells the player a code, privately: `[^_^] GOOD CITIZENS DO THEIR PART! Your link code is
K7P-42Q: in SPR on Discord, type /link K7P-42Q within 10 minutes. Only you can see this.` Typing `/link` with it in
the Discord ties the two, and the player's stats show under their name there. A code works once; three can be asked
for in an hour. A player already linked is told who to, and `/unlink` in Discord undoes it. A link changes nobody's
rank. See [discord.md](discord.md).

## Acting from Discord
With the Discord bot, staff can do all this without being in a game: the buttons on a
card, a slash command for each of these commands, and lines typed in a console channel.
[discord.md](discord.md) says how to set them up. They run these same commands, by the
same rules:

- **Rank comes from `[ranks]` in `discord.toml`:** the Discord roles you've mapped onto
  moderator, admin and the rest, and faculty by Discord user ID. Being a moderator in game
  isn't being one in Discord, or the other way round, as Discord doesn't know who a player
  is. What each rank may use is still `commands.toml`, and nobody acts on someone of their
  own rank or above. On probation, what names a player is a request. With `[automod]
  across_servers` on, a role `[ranks]` gives on only some servers counts on every server,
  as a rank in game does.
- **Players read their name in that Discord** ("Bravo was kicked by Mod"), or whatever
  `shown_as` in `[commands]` says, which can be "a moderator". `!whois` shows it too, with
  how it was given as for one typed in game (`!kick`).
- **A slash command is the `!` one,** whatever the slash looks like: `/kick` in Discord
  is `!kick`, and reaches every server, as the game's own `/kick` in game chat does too.
- **Some have another name there,** where the game's would mislead: `/letback` is
  `!pardon`, `/bannedwords` `!rules`, `/bannednames` `!bannames`, `/clearstrikes`
  `!pardonplayer`, `/reloadserver` `!reload`. `!calladmin` and `!strikes` aren't there; `/report` and `/tickets` have their
  own forms, for `!report` and `!reports`.
- **Someone who's left** can be kept off with the Kick button, or `/kick` with a time and
  picked from the list as you type (in game, `!kick` with a time and their player ID or
  an `@number`).

## Every command
| Command | Default role | |
| --- | --- | --- |
| `!help [command]` | everyone | The commands you can use, or how to use one |
| `!strikes [player]` | everyone | Your strikes (real ones, not a dry run's); moderators can name anyone |
| `!calladmin [why]` | everyone | Call the staff on Discord. Only while Discord can take it: see [Telling the staff](#telling-the-staff) |
| `!report <player> [why]` | everyone | Tell the staff on Discord about a player. Only while Discord can take it: see [Telling the staff](#telling-the-staff) |
| `!mods` | everyone | Which staff are in a game now. Only with the Discord bot on |
| `!stats [player]` | everyone | A player's record on these servers, from every game reported: yours, left out |
| `!season [number]` | everyone | Red against Blue in the season running (or just ended), or a past one's |
| `!link` | everyone | A code for `/link` in your Discord, told only to you. Only with the Discord bot's `[link]` on |
| `!side <red \| blue \| off>` | member | The team you're always put on. Only with the Discord bot's `[sides]` on |
| `!togglesides` | member | Stop being put on your side, or start again |
| `!respawn [reason]`, `!respawn <player> [reason]` | everyone; admins for someone else | Stuck? Respawns you on your own team: Huragok moves you to another team and straight back, as the game's admin menu does (the server has no kill command). Everyone reads `[o_o] DETOUR! PROCEED WITH CAUTION! Bravo respawned on Red team: stuck in a wall.` Team games during a round only: not free-for-all, and not a team's last player (the server refuses, and you read why). Once a minute for your own; admins aren't held to it, and name a player to respawn them (not someone of their own role or above). Not yet tried with players: whether the move back is a second respawn, and whether it counts as a death |
| `!players [name \| server \| all]` | moderator | Who's on, with `#numbers`, player IDs, team, time on and roles; `!players nate` lists only those with "nate" in their name (a busy server has more than one answer shows); `!players Minigames` lists another server's, and `!players all` every server's, each with an `@number` to act on them from here |
| `!whois <player \| ID> [lines]` | moderator | Their whole record, from every server: where they are, other names, what's on them now (a mute, a kick keeping them out, their bans as each server's list has them), every kick, mute, ban and warning with its case number (by `!`, `/`, Vergil, a vote or the console, who, why, and every server or the one server it stayed on), strikes on each line |
| `!warn <player> [reason]` | moderator | A private warning, recorded |
| `!mute <player> [time] [reason]` | moderator | Nobody hears their voice or reads their chat, on every server; someone on another server is muted there, and someone who left as they next join |
| `!unmute <player \| ID>` | moderator | On every server, wherever they are: on another server too, and someone who left; ends Vergil's own mute on them too |
| `!kick <player> [time] [reason]` | moderator | No time: the server keeps them out 10, 30, 60, then 120 minutes, the count starting again after a day without a kick, and every server keeps them off as long. A time: off every server that long, a moderator's for 1 day at most. `perm` is a ban. It reaches them on another server; for someone on no server it needs a time, and their player ID or an `@number`: see [Kicks with a time](#kicks-with-a-time-and-bans) |
| `!pardon <@number \| name \| ID \| IP>` | moderator | Let a kicked player back on every server now, wherever the kick was given. A kick with a time too: at moderator rank, one a moderator could have given, and a longer one takes an admin. On probation it's a request. `@number` from `!kicks` |
| `!kicks` | moderator | Who's been kicked here lately (two hours, since Huragok started), who a kick on another server keeps off this one, and every kick with a time for as long as it runs, with who, why and how long they're out; numbered for `!pardon` |
| `!mutes` | moderator | Who's muted: Vergil's list (strikes', moderators', admins') and the server's own; numbered for `!unmute` |
| `!novote <player> [time \| perm] [reason]` | moderator | They can't call votes: any vote they call is cancelled as it opens, and they're told why. They can still vote on other players' votes. No time means until `!allowvote`; see [Players who spam votes](#players-who-spam-votes) |
| `!allowvote <player \| @number>` | moderator | Give their votes back; `@number` from `!novotes` |
| `!novotes` | moderator | Whose votes are away, with who, why and how long; numbered for `!allowvote` |
| `!said <player> [lines]` | moderator | The last lines (5, up to 15) they typed in chat, on any server; kept 30 days |
| `!rules [line \| word]` | moderator | The word list: each line's name, words and what its strikes do; one line in full |
| `!bannames [text]` | moderator | The banned names (`names.txt`), numbered for `!unbanname` |
| `!strike <player> <list> [note]` | moderator | An automod strike, for something automod can't see, like voice: the list's next mute, kick or ban, on every server, whatever Vergil is on that server (a ban from a moderator goes to the admins as a request first). A line of the word list with no words is only for this: see [automod.md](automod.md#a-ladder-only-staff-climb) |
| `!strike <player> name [note]` | moderator | Bans their name (adds it to `names.txt`): they're kicked every time they join with it, until they change it. No strike, no ban |
| `!health` | moderator | How the server is running |
| `!promote <player>` | moderator | One step up the roles, up to the role below yours |
| `!demote <player>` | moderator | One step down, for someone below you: admin to moderator, moderator to probationary moderator, then member, member to player |
| `!roles` | moderator | Who has which role |
| `!requests` | moderator | The probationary moderators' requests waiting, on every server, and moderators' strikes that would ban (an admin's to confirm) |
| `!reports` | moderator | What's open for staff on Discord: reports and calls for an admin, on every server, how long each has waited, and whether someone's on it. Only while the full bot takes reports or calls |
| `!confirm <number> [command and its words]` | moderator | Confirm a request: it happens now, as yours; or name another command to do to that player instead (`!confirm 3 strike slurs`) |
| `!deny <number> [why]` | moderator | Turn a request down; the probationary moderator is told why |
| `!ban <player \| ID> [time \| perm] [reason]` | admin | Their ID, address and device, for good; works on players who left, by ID or a name Vergil has seen. A time makes it a kick that long: see [Kicks with a time](#kicks-with-a-time-and-bans) |
| `!unban <@number \| name \| ID \| IP>` | admin | Lift every ban on them, with everything linked to it, on every server, a server's own `/ban` too; typed on a server that doesn't have the ban, every other server is still told to lift it. With `across_servers` off, this server's alone, and the reply says where they're still banned; `@number` from `!bans` |
| `!bans [name]` | admin | The bans in force, newest first, with who banned, why, how long is left, and whether it's on every server or this one (one only this server's list has goes on every server's at the next look, within 10 minutes); a kick with a time is on the server's list too, and reads "kicked"; numbered for `!unban` and `!banall` |
| `!banall <@number \| player \| ID>` | admin | Put a ban from this server's list on every server's at once, rather than at the next look: on Huragok's list, with its time left, reason and who gave it; `@number` from `!bans` |
| `!changebanword <line \| word> \| mutes \| kicks \| ban` | admin | New steps for a line of the word list, its words and name kept: `!changebanword slurs \| 1d 7d \| 2, 1h 1d \| 5` |
| `!banword <words> <line>` | admin | Add words to a line of the word list (`automod.txt`), with its steps: `!banword word slurs` (`slur` finds `slurs`). A line that isn't there: the answer says how to make it |
| `!banword <words> \| [mutes] \| [kicks] \| [ban] \| <name>` | admin | Make a new line: `!banword word \| 1d 7d \| 2, 1h 1d \| 5 \| slurs`. Applies at once, in chat and to names with the word in them. Mutes, kicks and ban all blank: a line that only reports, a ticket for the staff on Discord and nothing in game. See [automod.md](automod.md#changing-the-word-list-from-the-game) |
| `!unbanword <words> [\| line]` | admin | Take words off `automod.txt` (a line left with no words goes too); `\| <line>` alone takes off the whole line |
| `!banname <#number \| name>` | admin | Ban a name (`names.txt`): a player on by `#number` or name has theirs banned and is kicked; any other text is banned as the name itself. Whoever uses it is kicked as they join, until they change it. No strike, no ban. A moderator's up to 0.4.4 |
| `!pardonplayer <player \| ID> [rule \| all] [reason]` | admin | Clear automod strikes and lift what automod did: unban (only Vergil's own bans), unmute. A word that isn't a line, nor `all`, is refused: a reason for clearing everything comes after `all` |
| `!unbanname <name>` | admin | Take a name off `names.txt` |
| `!say <text>` | admin | A `[Server]` line for everyone |
| `!map`, `!mode`, `!load`, `!nextmap` | admin | Change the game |
| `!endgame`, `!endround` | admin | |
| `!shuffle`, `!team <player> <color>`, `!teamcount [n]` | admin | Teams |
| `!startvote`, `!cancelvote`, `!passvote` | admin | Votes |
| `!maxping [ms \| off]`, `!servername [name]` | admin | Until the server restarts |
| `!restartserver [time \| gameover] [reason]` | admin | Restart this server, as the window's R does when players are on: everyone reads the usual countdown (`[restarts] warn_seconds`, 5 minutes by default) with the reason and who asked ("Server restarting in 5 minutes: lag (requested by Alice)."), and it goes at once if everyone leaves. **A time first** (`!restartserver 7m lag`, up to `60m`) counts down that long instead, with the usual warnings below it (5, 3 and 1 minutes, 30 seconds). **`gameover`** (`!restartserver gameover lag`) restarts once this game ends, a few seconds after (`[restarts] after_game_seconds`); everyone reads "This server restarts after this game: lag (requested by Alice)." Typed in the lobby, it's the game about to start. It waits for the game to end however long it runs. A countdown already under way (the daily restart's, a stop) keeps its time and ends in a restart. Only the server it's typed on. For a server running poorly that needs a fresh start |
| `!toggleglobal [on \| off] [time]` | admin | Global voice (the V key: everyone hears) on or off, on this server only. Alone it switches it over and it stays; `on` or `off` sets it. With a time (`30s`, `10m`, `2h`, `1d`) it switches back once that's up, either way: `!toggleglobal 10m` turns it off for 10 minutes, or on for 10 minutes if it was off. Written to the server's own block in `dedicated.toml`, which the server applies within seconds: no restart, nobody dropped. Switched back by hand to what `[defaults]` gives, a key only the toggles wrote comes out again, so a later change to `[defaults]` reaches the server (one you wrote there stays) |
| `!toggleteam [on \| off] [time]` | admin | Team voice (the B key: teammates hear), the same way |
| `!toggleproxy [on \| off] [time]` | admin | Proximity voice (the N key: players nearby hear), the same way |
| `!togglevoice` | admin | All voice chat off on this server for 10 minutes, then back on by itself; typed again, it's back on at once. Always 10 minutes: it's for stopping voice spam, so it can't be left off |
| `!admin [list \| add \| remove]` | faculty | The admins: `list` shows every server's as one list ("Admins on every server (3): …", with "(on Server 2's list)" for someone only on some servers' lists; with `across_servers` off, this server's own list); `add` makes a player an admin on every server, `remove` a moderator |
| `!reload` | faculty | Apply edits to `dedicated.toml` now |
| `!newseason [name] [length]` | faculty | End the season running, if any, and start the next: for good, or for a time (`30d`) |
| `!endseason` | faculty | End the season running now |

Everyone on the server reads each toggle as a `[Server]` line ("[-.-] PLEASE REMAIN CALM! Alice turned global voice (V) off for 10 minutes."), and again when a timed one switches back ("[^_^] YOUR TAX DOLLARS AT WORK! Global voice (V) is back on after 10 minutes."). A timed toggle is kept by Huragok through its own restarts; one whose time ran out while Huragok was down switches back as it starts. Someone changing the setting in the meantime (another toggle, or the window's D) wins: it isn't switched back. There's no `!togglechat`: the server's own lines are text chat, so with it off nobody could read why, or type the command to bring it back.

`!yes` and `!no` are the server's own votes, and lines starting with `/` are the
game's own commands, so neither can be used here.

On a Halo 3: ODST server (Project Reclaimer 0.9.19), which plays only what its file
names, the commands that change the game, teams or rounds (`!map`, `!mode`, `!load`,
`!nextmap <game>`, `!endgame`, `!endround`, `!shuffle`, `!team`, `!teamcount`,
`!startvote` but `!startvote kick`) answer "This server plays Halo 3: ODST: the
console cannot change its game, teams or rounds." without reaching the server.

## Kicks with a time, and bans

**A ban is for good.** Anything with a time is a kick: `!kick Bravo 7d cheating` removes them and
keeps them off every server for 7 days (with `[automod] across_servers` off, only this one). Each
time they try to join, the server tells them how much is left and why. `!pardon` lets them back
early. `!kick Bravo perm` is a ban, and needs the rank a ban does.

**`!kick` reaches them on any server.** For someone on another server, it's sent there. For
someone on none, it needs a time and their player ID or an `@number` from a list: by a name
alone it answers "Bravo isn't on any server now: their player ID (or @number from a list) names
them anyway.", and with no time "… so there's nobody to kick: a time keeps them off every server
as they join". An admin's `!ban Bravo 7d cheating` is a kick too; at the door it gives the reason
as typed, "cheating (every server)", where a `!kick`'s says "Kicked off every server for 7 days:
cheating". From Discord, the Kick button and `/kick` reach them too.

Without a time, `!kick` is the server's own: 10, 30, 60, then 120 minutes for repeat kicks, the
count starting again after a day without one. Every server keeps them off as long.

Underneath, a kick with a time is the server's timed ban, so it blocks their address and device
too, and holds while Huragok is down. It shows as a kick in `!whois`, `!bans`, `!kicks` and Discord,
and it doesn't take away anyone's role, as a ban does. The one place the word "banned" still shows
is the server's own lines: what it tells everyone as it happens, and what it tells the player at
the door: "You are banned from this server for another 6 days. Reason: Kicked off every server for
7 days: cheating".

A moderator's kick lasts one day at most: `moderators_kick_at_most` in `[automod]` changes that.
Admins and faculty have no limit. It holds on Discord's buttons too, and when a moderator confirms a
request. `!pardon` goes by the same line: at moderator rank it lifts what a moderator could have
given, and a longer one takes an admin.
