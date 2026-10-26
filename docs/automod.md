# automod

Vergil's automod acts on the words on your list: said in chat, they earn a strike,
and each strike is the next step on that word's line: kicks, then mutes, then a
ban, as you set it. In a name, they get the player kicked as they join, every time,
until the name changes. The list is one text file, `automod.txt` in the root (the
folder with `start.cmd`), with one rule per line in five parts:

```text
# words                 | kicks        | mutes          | ban | name
badword, otherword      | 1h           | 1, 1d, 7d      | 3   | slurs
rudeword                |              | 0, 1d, 3d, 7d  |     | rude
worstword               |              |                | 0   | threats
kickword
```

What a player gets each time:

| Line | 1st | 2nd | 3rd | 4th | after that |
| --- | --- | --- | --- | --- | --- |
| `slurs` | out 1 hour | muted 1 day | muted 7 days | banned for good | |
| `rude` | muted 1 day | muted 3 days | muted 7 days | muted 7 days | muted 7 days: never banned |
| `threats` | banned for good | | | | |
| `kickword` | kicked | kicked | kicked | kicked | kicked: never muted or banned |

**You don't have to write the lines yourself.** In the window, **D → A** lists the rules
as players meet them, adds one from the words and a ready-made ladder (or your own, a
question at a time), changes or takes one out, and tries a line of chat against them;
**D → N** does the same for banned names. Either way Vergil reads the file again
within seconds.

A strike is the next step down its line: the kicks come first, then the mutes, then
the ban. The numbers in **mutes** and **ban** count the strikes that come before
them, so `0` is the first offence: `| | | 0 |` bans the first time, `| | 0, 1d |` mutes
the first time. They can be as large as you like; the only check is that every kick
and mute time you list comes before the ban.

**A kick and a mute on the same strike.** List more kick times than there are
strikes before the mutes, and the ones past the start of the mutes go with them:
that strike removes the player for its kick time, then mutes them once they're back
(the mute's time starts when the kick's ends, so a short mute isn't used up while
they're out). Once the kick times run out, the mutes go on alone:

```text
badword | 1d, 2d, 3d, 4d, 5d, 6d, 7d | 5, 1d | 10 | slurs
```

is 1st to 5th: out 1 to 5 days; 6th: out 6 days, then muted 1 day; 7th: out 7 days,
then muted 1 day; 8th to 10th: muted 1 day; 11th: banned for good. Before the mutes,
the last kick time repeats as it always has (`1h | 3, 1d` is out an hour three times,
then muted); past their start, a kick time is used once.

| Part | |
| --- | --- |
| **words** | Separated by commas. List each word once in plain letters: capitals, accents, digits and symbols for letters (`b4dw0rd`, `b@dw()rd`), spacing and punctuation (`b a d w o r d`, `b?a!d+w%o&r.d`: up to four of anything that isn't a letter or digit between two letters), repeated letters, lookalike letters (Greek and Cyrillic ones, capitals too, small capitals like `ʙᴀᴅᴡᴏʀᴅ`, fullwidth, styled, circled and boxed letters from "fancy text" makers), invisible characters, and plurals are caught for you. Keep doubled letters doubled. `word*` also catches it at the start of longer words, `*word*` anywhere inside one (use that for long words only). `-word` is never caught by that line. |
| **kicks** | How long each kick keeps them out, in order: `30m`, `1h`, `1d`, `2w`. The last one repeats until the mutes start. Blank: the server's own kick, which keeps them out 10, 30, 60, then 120 minutes. Times past the start of the mutes kick, then mute (above). At most one time per strike before the ban. |
| **mutes** | How many strikes come before the mutes (`0`: muted the first time), then how long each mute lasts, in order: `0, 1d, 3d, 7d`. The last one repeats; `perm` (last only) is muted for good. Blank: never muted. At most one time per mute before the ban. |
| **ban** | How many strikes come before the ban for good (`0`: banned the first time). Blank: never banned. |
| **name** | One word that players read and moderators type in `!strike`, so nobody has to type the word itself. Optional, but name every line whose strikes add up (it mutes, bans, or has more than one kick time): a line without one is `rule1`, `rule2`… by its place in the list, its strikes are kept under that, and they move to another line if you add a line above it. `check`, the start and each reload point such lines out. It can't be `name`, which `!strike` keeps for names. |

**A number is never taken for a word.** With a three-letter word listed, "respawn
in 45s", "455 kills", "4:55 left" and "Team 455" aren't caught: a match with more
digits than letters doesn't count for a word that has more letters than digits. The
other side of it (a decision you can ask to have changed): a disguise with more
digits than letters, like `a55`, isn't caught either, while one with at least as
many letters as digits (`b4d`) and one made of symbols still are. A listed word that
is all digits, like a hate number, is matched as written.

The first start writes the file with these notes at the top. Edits
apply within five seconds while Huragok runs. If a line has a mistake, the
log says which line and what's wrong, and the list from before stays in force. A
word that is nothing once accents and invisible characters are taken out (a
zero-width space or a lone accent pasted from the web) is such a mistake: it would
catch every line of chat, so the line is refused, named with the character's code.
Save the file as UTF-8 (with or without a BOM) or as Notepad's "Unicode"; one saved
as ANSI is refused with "save it as UTF-8 in your editor". While an editor is still
saving it, the old list stays and the file is read again a moment later.

### Lines from before mutes
Before mutes, a line had four parts: `words | timers | chances | name`. A line
like that (four parts, the last a name) is still read the way it always was, its
chances as the ban: `badword | 1h, 1d, 7d | 3 | slurs` is the same as
`badword | 1h, 1d, 7d | | 3 | slurs`. `start.cmd check` points those lines out. A
three-part line (`words | timers | chances`) is a mistake now, since the third
part is the mutes; the log says to move the number to the fourth part.

## Mutes
A muted player stays in the game, but nobody hears their voice or reads their chat
(the server stops relaying both). The server forgets its mutes when it restarts, so
Vergil keeps its own list: when a muted player joins any server, Vergil mutes them
again for what's left, and they read `KEEP IT CLEAN! You're still muted (rude): 2
days 23 hours left.` A player already on whom the server shows as unmuted is muted
again within five seconds: after the server restarted while Huragok was closed,
after Huragok's console reconnects (everyone on is looked at again), and when a
moderator's shorter mute runs out while Vergil's has longer to go. A strike on
someone who isn't on (`!strike` by player ID) mutes them as they next join. The
time counts from the strike, on or off.

`!unmute`, from anyone, ends the mute for good: Vergil won't put it back, though
the strike still counts toward the next step. It reaches a player who has left
too (by a name Vergil has seen them use, or their player ID). `!pardonplayer`
clears the strikes and unmutes them.

**A moderator's `!mute` goes on the same list.** `!mute Bravo 1d spam` meant a day,
but the server forgot it at its next restart; now Vergil mutes Bravo again as they
join until the day is up, and they read `KEEP IT CLEAN! You're still muted (muted by
Alice: spam): 23 hours left.` A `!mute` with no time lasts until `!unmute`. A newer
`!mute` replaces the older one (a moderator shortening a mute gets the shorter one),
`!unmute` ends it, and `!strikes <player>` shows it (`Muted (!mute by Alice: spam)`).
It's no strike, and `!pardonplayer` leaves it alone: it isn't automod's. Vergil puts
it back whatever it is on the server they join (on, dry run or off), since it's the
moderator's call, not Vergil's. The same goes for a mute from anywhere else: an admin's
own in game, the relay, the console (the server announces each one; Vergil's own
aren't counted twice).

**Someone who left** can be muted too: `!mute <name> 1d` for a player who isn't on
puts the mute on the list for their player ID, and it starts as they next join (the
day counts from now). Someone else who takes the name meanwhile isn't muted: when
several players have used the name, the moderator picks the one with an `@number`
(see [commands.md](commands.md#using-commands)). Someone on another server is that
server's to mute.

**Across servers** (`across_servers`, on by default): a mute on the list follows the
player to any server they join. With it off, a mute is put back only on the server
it was given on, and a strike's kick, mute or ban lands only there.

`!mutes` (moderators) lists everyone muted now, numbered: `!unmute @2` lifts that one.

## Names
A name is never struck, muted or banned: the name is the problem, so the player is
kicked as they join, every time, until they change it. Vergil lets them straight
back in after each kick (the server's kick would otherwise keep them out 10 minutes
or more), so they can come back as soon as they've picked another name. They read
`KEEP IT CLEAN! Change your name, it isn't allowed here (banned name). Join again
with another one.`, whichever list caught it.

A name is kicked when it has:

- a word from `automod.txt` in it (with `check_names = true`, the default), or
- a name from `names.txt` in it: Vergil's banned names, one per line.

So one list does both: a word you put in `automod.txt` is struck in chat and keeps
a name with it out of the game. Players write their names run together, so how a
word catches a name is a setting, `name_match` under `[automod]`:

| `name_match` | `hitler` listed catches | Doesn't catch |
| --- | --- | --- |
| `"anywhere"` (the default) | Hitler, Adolf Hitler, DasHitler, HiElleHitler, xXhitlerXx, HITLERFAN, H1tl3r | |
| `"words"` | Hitler, Adolf Hitler, DasHitler, HiElleHitler, H1tl3r | dashitler, xXhitlerXx, HITLERFAN |

`"anywhere"` catches the word anywhere in a name, as if it were listed `*word*`. A
short word then catches more than you mean (`ass` in Classic and Assassin), so list
the names that should pass on its line with a minus: `ass, -classic, -assassin`.
`"words"` takes the name apart where its capitals and digits change (DasHitler is
"Das Hitler", Sniper1488 is "Sniper 1488") and then wants the word on its own, as
in chat: fewer names caught by mistake, but a name written all in one case slips
through. Either way a listed number is never caught inside a longer one: `88`
catches Sniper88 and xX88Xx, not Mike1988. In the window, D → N → T tries a name
against both lists.

`names.txt` is for a name that isn't a word to ban in chat: a player impersonating
someone, say. Its names are matched as written (inside a longer name too, as a word
of their own), whatever `name_match` says. `!banname <#number | name>` (admins)
adds a name to it: a player on the server named by `#number` or exact name has
theirs banned and is kicked; any other text is banned as the name itself.
`!strike <player> name [note]` (moderators) does the same for a player, with a
note. Anyone joining with that name after them is kicked too. The same disguises are caught as in
`automod.txt` (`BR4VO` for `Bravo`), and so is the name inside a longer one; `*name*`
also catches it inside a word. Vergil writes the date, the moderator and their note
above each name it adds:

```text
# banned 2026-10-18 by Alice: offensive
Bravo
```

Edit `names.txt` like `automod.txt`: changes apply within five seconds, and anyone
already on with a name you add is kicked then. `!unbanname <name>` (admins) takes
one off; if the name is also caught by `automod.txt` or by a wider entry like
`*name*`, it says which.

The server announces each kick in the game's chat, with the name, as it announces
each join; that can't be switched off from here.

## Changing the word list from the game
Admins add words without leaving the game. Most of the time the line is there
already (its steps set up once), and a new word just joins it:

```text
!banword newword slurs
```

The last word is the line, the words before it join it (several with commas:
`!banword word1, word2 slurs`), and from then on they lead down that line's steps,
counted with its other words. `slur` finds `slurs` too. A line that isn't there yet
gets an answer saying so, with the lines there are and the whole command to make
it, your word already in it; a word with no line gets asked which.

A new line is written out in full, as `automod.txt` has it:

```text
!banword newword | 10m 30m 1h 1d 7d | 5, 1h, 1d, 3d, 7d | 10 | hate
```

That's out 10 minutes, 30 minutes, an hour, a day, a week; from the 6th strike
muted an hour, a day, 3 days, then a week each time; banned on the 11th. In game the
times can be split by spaces or commas; Vergil writes them into `automod.txt` the
way the file has them, as a new line at the end, and the change applies at once:
in chat, and to anyone on now whose name has the word in it. The last part is the
line's name (one word: `racist slurs` is written `racist-slurs`).

- `!banword newword, otherword | slurs` (with a bar) adds to the `slurs` line too.
  So does the whole line with that name and the same ladder (or the ladder left
  out); another ladder under a name already used is refused, with what that line
  does now (`!changebanword` changes a line's steps).
- A line that kicks every time and nothing more: `!banword newword | | | | kicks`.
- A word the list catches already, disguised or not (`b4dw0rd` when `badword` is
  listed), isn't written twice: the answer says which line has it.
- A mistake (a time like `1x`, more kick times than strikes before the ban, a line
  called `name`) is answered with what's wrong, and nothing is written.

`!unbanword <word>` takes a word off, as the file lists it; a line left with no
words goes too. If the word isn't listed itself but is caught by another entry
(`b4dw0rd`, caught by `badword`), it says which entry, and that `-b4dw0rd` on that
line lets it through. Strikes already given stay; `!pardonplayer` clears them.

Everyone in the chat sees the command line, the word included, as with every
command. The window's D → A changes the list without that.

## What players read
The reason they see, without ever repeating the word:

> KEEP IT CLEAN! That isn't allowed here (slurs, strike 2 of 3): muted for 1 day. Next time: muted for 7 days.

`strike 2 of 3` counts toward the ban; a line with no ban says `strike 2`. A timed
kick is a timed ban underneath, because the server's own kick times are fixed, so
the server's announcement says "banned by Vergil" (the `name` setting below); the
reason says how long.

One line counts once: a line repeating a word twenty times is one strike, and so
are lines sent within `cooldown_seconds` of each other. A word split over two lines
sent within `split_seconds` is caught. When a line matches more than one rule, the
one with the heaviest next step counts (a ban, then a mute, then a kick; longer
beats shorter). A muted player's chat never reaches Vergil, so it doesn't add
strikes. A moderator's command line isn't chat: `!strike Bravo slurs he said <the
word>` from someone who may use `!strike` (or `!mute`, `!warn`…) doesn't strike the
moderator. A player's own commands still count, so `!help <the word>` is struck.

Tested against a real night's chat: the n-word on one line caught all four lines
that used it (digits for letters, spaced out, a slang plural, and one mixed with
another slur) and a player's name, with nothing caught by mistake in 761 lines.

## Vergil on each server
Each server has its own Vergil, on its page in the window (D → the server →
Huragok's own → `automod`), applied at once:

| | What Vergil does on that server |
| --- | --- |
| `on` | Acts on the word list in chat and in names: strikes, kicks, mutes, bans |
| `dry_run` | Watches and logs what it would do, sends nothing; its strikes there are kept apart and never count where it's on |
| `off` | Leaves that server's chat and names alone; `!strike` answers that Vergil is off there |

A server without its own follows `[automod] dry_run`. Either way, mutes already
decided (a moderator's, an admin's, a strike's where Vergil is on) are put back on
it as `across_servers` says, and the moderators' commands work as everywhere. The
window's top line shows each server's (`VRGL … automod (on: Alpha; dry run: Bravo)`),
and so does the status card (V → Safety → Vergil).

## Commands
| Command | Default role | |
| --- | --- | --- |
| `!strikes` | everyone | Your own strikes, what happens next, and how long a mute has left |
| `!strikes <player>` | moderator | Someone else's |
| `!strike <player> <rule> [note]` | moderator | A strike on the line with that name, for something automod can't see, like a slur in **voice**: its next kick, mute or ban |
| `!strike <player> name [note]` | moderator | Bans their name: adds it to `names.txt` and kicks them. No strike, no ban |
| `!banword <words> \| [kicks] \| [mutes] \| [ban] \| [name]` | admin | Adds a line to `automod.txt`, or `!banword <words> \| <line>` adds words to a line: see above |
| `!unbanword <word>` | admin | Takes a word off `automod.txt` (a line left with no words goes too) |
| `!banname <#number \| name>` | admin | Bans a player's name (kicking them), or a name itself, in `names.txt`. No strike, no ban. A moderator's command up to 0.4.4 |
| `!pardonplayer <player \| player ID> [rule \| all] [reason]` | admin | Clears their strikes (all, or one named line's) and lifts what automod did: unbans, unmutes, and lets a kicked player back in |
| `!unbanname <name \| @number>` | admin | Takes a name off `names.txt`; `@number` from `!bannames` |
| `!rules [line \| word]` | moderator | The word list: each line's name, words and what its strikes do (the answer is yours alone); `!rules slurs` shows one line in full |
| `!bannames [text]` | moderator | The banned names in `names.txt`, newest first, numbered for `!unbanname` |
| `!changebanword <line \| word> \| kicks \| mutes \| ban` | admin | New steps for a line, its words and name kept (its strikes count toward the new steps): `!changebanword slurs \| 30m 2h \| 1, 1d 3d \| 5`. A part left blank is none: `!changebanword rude \| \| 0, perm` mutes for good from the first strike |
| `!mutes` | moderator | Everyone muted now: Vergil's list and the server's own, numbered for `!unmute` |

`!strike`, `!strikes` and `!pardonplayer` work on players who aren't on the server:
name them by player ID (or its first 8 characters or more), by a name Vergil has
seen them use, or by an `@number` when that name is more than one player's. The
start of a name alone picks nobody: `!strike bobb slurs` answers "Nobody here is
called 'bobb'. Did you mean Bobby (#4)? Use #4." rather than striking Bobby, who may
not be the one meant. See [commands.md](commands.md#using-commands) for naming
players. A strike's kick, mute or ban lands on the
server the player is on now, wherever the moderator typed it; a kick for someone on
no server kicks nobody (the strike still counts, and the reply says so), and a mute
starts as they next join.

`!pardonplayer` lifts only what automod did. It looks at the server's ban list and
lifts a ban only when Vergil gave it (the list names the same one acting as the
strike did, within two minutes of it); when someone else banned that player too, it lifts nothing
and says whose ban stays (`!unban` lifts both). If the ban list can't be read,
nothing is lifted. The server's own `pardon` (`!pardon`) only lets a kicked player
back early; it doesn't touch automod's strikes.

Who can use each one is set in `commands.toml`, like every command: see [commands.md](commands.md).

## Dry run
automod starts in **dry run** because the first start writes `dry_run = true` under
`[automod]` in `settings.toml`: it counts strikes and logs what it would do, but
doesn't kick, mute, ban or tell anyone. A `settings.toml` without that line (or
without an `[automod]` section at all) runs automod live, so keep the line until
you mean it. Watch its lines for a while (in the window they're tagged `VRGL`; in
`logs/huragok.log` they start `part.automod:`), then set `dry_run = false` and start
Huragok again (`settings.toml` is read as it starts), or switch one server at a
time on its page in D (applied at once, see [Vergil on each server](#vergil-on-each-server)).
Dry-run strikes and mutes are kept apart: going live starts everyone from zero. `!strike <player> name`,
`!banname`, `!banword` and `!unbanword` still change their file in dry run (the
lists are yours either way); nobody is kicked for it until dry run is off.

## Settings
In `settings.toml`, under `[automod]` (in the file's VERGIL part; the section kept
its name):

| Setting | Default | |
| --- | --- | --- |
| `enabled`, `dry_run` | | Switch it on; act, or only record what it would do |
| `words` | `"automod.txt"` | The word list |
| `names` | `"names.txt"` | The banned names |
| `forget_after_days` | `0` | Strikes older than this stop counting; `0` means never. A mute runs its time either way |
| `name` | `"Vergil"` | Who players read as acting ("banned by Vergil"). A `settings.toml` written before 0.4.0 says `"AutoMod"` |
| `exempt_roles` | `["admin", "owner"]` | Never acted on, in chat or by name |
| `check_names` | `true` | Kick names with a word from `automod.txt` in them. `names.txt` applies either way |
| `name_match` | `"anywhere"` | How a word from `automod.txt` catches a name: `"anywhere"` in it, or `"words"` (only as a word of its own, the name taken apart at its capitals and digits). See [Names](#names) |
| `across_servers` | `true` | Huragok's moderation (`!` commands, Vergil, the relay, the window's console) reaches every server: a mute follows the player to any server, a kick keeps them off every server until its time is up, a ban goes on Huragok's own list and every server's ban list (a new server's too), and so does an admin Huragok makes. The game's own `/kick` and `/ban`, and votes, stay on their server (only written on the player's record; `!banall` puts a `/ban` on every server). `false`: Huragok's stay on theirs too |
| `cooldown_seconds` | `10` | Lines this close together count as one strike |
| `split_seconds` | `10` | Catch a word split over lines this close together |
| `messages` | | Change what players read: `chat`, `name`, `manual`, `next`, `muted`, using `{detail}`, `{label}`, `{strike}`, `{action}`, `{next}`, `{by}`, and `{left}` in `muted`. For a brace players should read, write `{{` or `}}`: `check` names a message with a stray one, and while running such a message falls back to the default rather than stopping automod |

`enabled` is on unless you set it off; `dry_run` is off unless you set it on (the
first start writes it on, as above).

Strikes and Vergil's mutes are kept in Huragok's database (`records/huragok.sqlite3`
by default, the `database` setting) with the line that caused them, and every
action is in the shared record with its reason. There's one database per root (the
folder with `start.cmd`), so the mutes follow a player to every server this
Huragok runs. Other parts can listen for `automod.strike`, `automod.pardon`,
`automod.name`, `automod.name_banned`, `automod.name_allowed`,
`automod.words_banned`, `automod.word_allowed` and `automod.rule_changed` events.
