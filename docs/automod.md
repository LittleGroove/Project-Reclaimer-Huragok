# automod

Removes players for words on your list, in chat and in names as they join. The
list is one text file, `automod.txt` in the supervisor's folder, with one rule per
line: the words, how long each offence keeps a player out, and how many chances
they get before a ban.

```text
# words                 | timers       | chances | name
badword, otherword      | 1h, 1d, 7d   | 3       | slurs
kickword
worstword               |              | 0       | threats
```

What a player gets each time:

| Line | 1st | 2nd | 3rd | 4th, and after |
| --- | --- | --- | --- | --- |
| `slurs` | out 1 hour | out 1 day | out 1 week | banned for good |
| `kickword` | kicked | kicked | kicked | kicked: never banned |
| `threats` | banned for good | | | |

`chances` is how many times a player is removed and let back; the next time is
the ban. So 3 chances means banned on the 4th, and `0` is zero tolerance.

| Part | |
| --- | --- |
| **words** | Separated by commas. List each word once in plain letters: capitals, accents, digits for letters (`b4dw0rd`), spacing (`b a d w o r d`), repeated and lookalike letters, and plurals are caught for you. Keep doubled letters doubled. `word*` also catches it at the start of longer words, `*word*` anywhere inside one (use that for long words only). `-word` is never caught by that line. |
| **timers** | How long each offence keeps them out, in order: `30m`, `1h`, `1d`, `2w`. The last one repeats. Blank: the server's own kick, which keeps them out 10, 30, 60, then 120 minutes. |
| **chances** | How many times they're removed and let back; the next time is a ban for good. Blank: never banned. `0`: zero tolerance, banned the first time. At most one timer per chance. |
| **name** | One word that players read and moderators type in `!strike`, so nobody has to type the word itself. Optional; lines without one are `rule1`, `rule2`… |

The first start writes the file with these notes at the top. Edits
apply within five seconds while the supervisor runs. If a line has a mistake, the
log says which line and what's wrong, and the list from before stays in force.

automod starts in **dry run** (`dry_run = true` under `[automod]`): it counts
strikes and logs what it would do, but doesn't act. Watch `logs/supervisor.log` for a
while, then set `dry_run = false`.

## What players read
The reason they see when removed, without ever repeating the word:

> That isn't allowed here (slurs, strike 2 of 3): removed for 1 day. Next time: removed for 7 days.

A timed removal is a timed ban underneath, because the server's own kick times are
fixed. The server's announcement therefore says "banned by AutoMod"; the reason
above says how long.

One line counts once: a line repeating a word twenty times is one strike, and so
are lines sent within `cooldown_seconds` of each other. A word split over two lines
sent within `split_seconds` is caught. When a line matches more than one rule, the
one with the heaviest next step counts.

Tested against a real night's chat: the n-word on one line caught all four lines
that used it (digits for letters, spaced out, a slang plural, and one mixed with
another slur) and a player's name, with nothing caught by mistake in 761 lines.

## Commands
| Command | Default role | |
| --- | --- | --- |
| `!strikes` | everyone | Your own strikes, and what happens next |
| `!strikes <player>` | moderator | Someone else's |
| `!strike <player> <rule> [note]` | moderator | A strike on the ladder of the line with that name, for something automod can't see, like a slur in **voice** |
| `!pardonplayer <player \| player ID> [rule \| all] [reason]` | admin | Clears their strikes (all, or one named line's) and lifts what automod did: unbans, and lets a kicked player back in |

`!pardonplayer` works on players who aren't on the server: name them by player ID,
or by a name the supervisor has seen them use. The server's own `pardon` (`!pardon`)
only lets a kicked player back early; it doesn't touch automod's strikes.

Who can use each one is set in `commands.toml`, like every command: see [commands.md](commands.md).

## Settings
In `settings.toml`, under `[automod]`:

| Setting | Default | |
| --- | --- | --- |
| `enabled`, `dry_run` | | Switch it on; act, or only record what it would do |
| `words` | `"automod.txt"` | The word list |
| `forget_after_days` | `0` | Strikes older than this stop counting; `0` means never |
| `name` | `"AutoMod"` | Who players read as acting |
| `exempt_roles` | `["admin", "owner"]` | Never acted on |
| `check_names` | `true` | Check names as players join |
| `cooldown_seconds` | `10` | Lines this close together count as one strike |
| `split_seconds` | `10` | Catch a word split over lines this close together |
| `messages` | | Change what players read: `chat`, `name`, `manual`, `next`, using `{detail}`, `{label}`, `{strike}`, `{action}`, `{next}`, `{by}` |

Strikes are kept in the supervisor's database with the line that caused them, and
every action is in the shared record with its reason. Dry-run strikes are kept
apart: going live starts everyone from zero. Other parts of the supervisor can listen for
`automod.strike` and `automod.pardon` events.
