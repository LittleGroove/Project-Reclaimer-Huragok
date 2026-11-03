# The Discord bot

Huragok doesn't come with a bot everyone shares. **You make your own bot** in Discord's
developer portal and give Huragok its token; Huragok runs it for you, and you own it.
This guide goes from nothing to a bot that answers in your Discord. It takes about
ten minutes.

Nothing changes until you set it up: `discord.toml` starts with `mode = "off"` and every
feature off, and with the mode off nothing goes to Discord. Once it's on, and you've
switched on what you want:

- the feeds post what happens on your servers, each kind of message where you say;
- a status board shows how the servers are, with pings for people who want them;
- staff act from Discord: buttons on each card, every in-game command as a slash command,
  a typed console, and notes and flags on players;
- players reach staff: `!report` and `!calladmin` in game, `/report`, `/appeal` and a Contact
  button in Discord.

It doesn't link Discord accounts to players, sync roles, keep stats or leaderboards, carry
game chat, run minigames, or moderate the Discord itself.

## What you need
- Huragok 0.6.0 or newer, running, with the bot's connection out to the internet. No
  port to open and no website: the bot connects out to Discord, as a game client does.
- A Discord you can manage (you own it, or you have **Manage Server** there).
- A bot of its own for each Huragok. Two Huragoks on one token would both try to answer
  every command.

Only want posts in a channel, with no bot at all? See
[Webhook only](#webhook-only-no-bot).

## 1. Make the bot
All of this is at <https://discord.com/developers/applications>, signed in to the account
that should own the bot.

1. Click **New Application**, give it a name, and create it. The name is what people see.
   You can add a picture on its **General Information** page.
2. Open its **Installation** page and set **Install Link** to **None**. Save. Do this
   first: Discord won't let a bot go private while it has an install link, and says
   "Private application cannot have a default authorization link". Leave **Guild Install**
   ticked under Installation Contexts.
3. Open its **Bot** page:
   - Switch **Public Bot** off, so nobody else can add your bot to their Discord.
   - Leave **Requires OAuth2 Code Grant** off, or the invite link won't work.
   - Leave **Presence Intent**, **Server Members Intent** and **Message Content Intent**
     off for now. **Message Content Intent** is for the typed console, where
     `/huragok check` says so if it's missing, and for Contact staff's transcripts, where
     the card says so when a thread closes without one.
   - Click **Save Changes**.
4. Still on the **Bot** page, click **Reset Token** and copy the token. Discord shows it
   only once, and may ask for your two-factor code first.

**The token is a password.** Whoever has it can act as your bot. Don't post it, don't
put it in a screenshot, and don't send it to anyone helping you. If it ever gets out,
**Reset Token** makes a new one and the old one stops working; give Huragok the new one
as in step 5.

## 2. Give Huragok the token
In Huragok's window:

5. Press **J**, then **M**, then **3**. Paste the token (Ctrl+V or right-click) and press
   Enter. It shows as stars. Huragok asks Discord about it before keeping it, and says
   "Signed in as" your bot's name. It's kept in `discord.toml` in Huragok's folder, so
   keep that file to yourself.
6. The bot connects within a few seconds. The top of **J** says how it's doing; press Esc
   and J again to see it change.

Rather not use the window? Put `mode = "bot"` and `token = "..."` at the top of
`discord.toml` (**O** in J opens it). Huragok reads the file again within seconds of it
changing. Or keep the token out of the file: `token_variable = "NAME"` reads it from that
environment variable, which only changes when Huragok starts again. With no window at all
(Docker), that's the way: the variable under `environment:`, and your Discord user ID under
`owners` in `[ranks]`, since there's no window to give you a claim code.

## 3. Add the bot to your Discord
7. Copy the invite link Huragok printed after the token, and open it in a browser. If it
   wrapped badly in the window, it's also in `logs/window.log`. To get it again: **J**,
   **V**.
8. Pick your Discord, click **Continue**, then **Authorize**. The link asks for exactly what
   the bot uses now, so leave them ticked:

   | Permission | What it's for |
   | --- | --- |
   | View Channels | seeing the channels it posts in |
   | Send Messages | posting |
   | Embed Links | cards |
   | Attach Files | long answers, as a file |
   | Read Message History | finding its own posts again |
   | Manage Webhooks | posting as the Superintendent, Vergil and Huragok; without it, it posts as itself |

   Discord gives the bot a role of its own, named after it.

Features you switch on later can want more:

| What's on | What the bot's role needs |
| --- | --- |
| **Discuss** on cards (`threads` in `[tickets]`), or the console's answers in threads | Create Public Threads, Send Messages in Threads |
| Cards with threads of their own (`when_closed` or `closes_to` set to `thread`) | Manage Threads too |
| The typed console | Pin Messages for its menu; Manage Messages to clear itself, delete a typed password, and remove lines from people without a rank |
| Contact staff | Create Private Threads, Send Messages in Threads, Manage Threads; Pin Messages to pin its button, which works unpinned too |
| The bot's nickname | Change Nickname |
| Channel names and topics | Manage Channels |
| Voice channel status | Set Voice Channel Status, Manage Channels |

Tick them on the bot's role yourself in Server Settings → Roles, or **J**, **V** makes a new
link that asks for what's on now: open it with the bot still there. **Don't remove the bot to
do this.** Discord deletes its slash commands and webhooks with it. The commands come back by
themselves when it's added again, but cards it had already posted can't be edited after that,
and stay until you delete them by hand. `/huragok setup` says what's still missing, and offers
the link while something is. `/huragok check` warns if the role has Administrator, Ban Members or
Kick Members, none of which the bot uses.

## 4. Make yourself its owner
9. In the window, press **J**, then **K**. It shows a code that works once, for 15 minutes.
10. In your Discord, in any channel, type `/huragok claim` and the code. If `/huragok`
    doesn't come up, reload Discord with Ctrl+R. Only you see the answer.

That makes you an owner of the bot, and makes that Discord the bot's own. Owners are the
people who can use `/huragok`. They're named by user ID, never by a role, because anyone
with Manage Roles could hand out a role. To add others: a new code for each (**J**, **K**), or
**Owners** in `/huragok setup`. From Discord an owner acts at owner rank on every server: any
command, on anyone. If that's more than someone setting up the bot should have,
`owners_in_game = "admin"` in `[ranks]` (or **Owners** in `/huragok setup`) keeps them to admin.
It gives them no rank in the game itself: there, `commands.toml` decides.

## 5. Set up your Discord
**Roles.** In Server Settings → Roles, make the ones you'll map onto Huragok's ranks, for
example `@Server Admin`, `@Server Mod`, `@Probation` and `@Member`, and give them to
people.

**Channels.** Everything the bot posts is a ticket, a record, or a post for everyone, and the channel decides which: send a message to `staff` and it's a ticket, to `logs` and it's a record. Staff's side only needs `staff` and `logs`: `reports` and `alerts` fall back on `staff`, and `huragok-logs` on `logs`. The others have nothing to fall back on, and what posts there waits and says so. Make the ones you'll use.

| Name in Huragok | For | Who should read it | Example |
| --- | --- | --- | --- |
| `status` | the server cards, and nothing else | everyone; **nobody types** | `#servers` |
| `info` | news: pings people asked for, seeding, restarts planned, updates. Contact staff's button goes here unless you say | everyone; **nobody types** | `#server-news` |
| `games` | games ending with at least `min_players` on, and starting too once `games-started` is switched on in **Messages**; a playlist put in rotation by the player count (`games-playlist`, see G → P in the window) | everyone; **nobody types** | `#games` |
| `staff` | tickets: what Vergil did, who to keep an eye on | **staff only** | `#staff-tickets` |
| `reports` | tickets: people asking staff for something. Unset, these go to `staff` | **staff only** | `#reports` |
| `appeals` | tickets: people appealing what's against them. Unset, these go to `reports` | **staff only** | `#appeals` |
| `alerts` | tickets: what's wrong with the servers now. Unset, these go to `staff` | **staff only** | `#alerts` |
| `logs` | the record of what people did: moderators' actions, closed tickets, votes, joins, settings | **staff only** | `#logs` |
| `huragok-logs` | the record of what Vergil and the Superintendent did. Unset, these go to `logs` | **staff only** | `#huragok-logs` |
| `console` | the console from Discord | **staff only** | `#console` |

**A channel nobody types in:** in its settings → **Permissions**, take **Send Messages** away from `@everyone`, then **add the bot's role and allow Send Messages for it**, or the bot can't post there either. `/huragok check` tells you if one's been missed.

**A private channel:** when you make it, switch on **Private Channel**, or in an existing
channel use its settings → **Permissions**. Then **add the bot's role** to it, or the bot
can't see it either. Staff channels go quickest in one private category: its channels take
the category's permissions, so you add the bot's role once, and your staff roles too. Huragok
refuses to post private things where @everyone can read, and says so. `/huragok check` also names
any other role that can read a staff channel, as a role everyone has is as good as @everyone. On a
Discord with only you in it none of that matters: a switch on the **Channels** panel lets them
(`public = true` under `[feeds]`).

**Tell Huragok.** Type `/huragok setup`. It says what's left to do, in order, with the next step on the green button:

- **Features:** what you want it to do, each with what it does and what it's still missing. **Recommended** on the first screen, there while nothing's switched on, turns on what staff need and nothing that posts where everyone can see: the moderation feed, Vergil's feed, alerts, calls for an admin, probationary moderators' requests, the audit log, the slash commands, and notes.
- **Channels:** which channel each name above means, called whatever you like in your Discord: `#spr-appeals` is `appeals` once it's picked here. Each pick answers at once: whether the bot can post there, and if not, what to do. **Unset one** clears one.
- **Ranks:** which roles count as admins, moderators, probationary moderators and members.
  Nothing counts until it's here: a Discord's own admins aren't your servers' admins. An
  admin role means admin on every server.
- **Who gets told:** the roles to ping when a server's in trouble, someone calls for an admin, and so on: as many as it's for, as ranks don't ping each other. Until then a card arrives and nobody's phone buzzes, though some make a sound for whoever has that channel's notifications on (`silent` below).
- **Say hello in every channel** posts a line in each, saying what it's for, so you can see they work and see the layout. They go by themselves after a few minutes.
- **Check:** everything the features you turned on need.
- **Owners, Messages, Tickets, Commands, Console, Reports, Appeals, Contact, Status, Notes:** each with its settings, and its switch where it has one. **Commands** is switched in Features. The Console's clearing is under **Clearing and more**.
- **Invite link:** on the first screen while the bot's role lacks something.

Each change takes effect straight away, and something you fix in Discord shows within moments, or five minutes at the most. **J** in the window (**R**, **N**,
**F**) does the same with numbered lists, and editing the file works too: all three end up
in the same place. `discord.toml` is read again within seconds of changing, with no restart; a few things need the bot to sign in again, which it does by itself.

**Ranks that aren't the plain ones.** These are in `[ranks]`, in the file:

- `discord_administrators`: what Discord's own Administrator permission counts as. `"nothing"` unless you change it, or `"moderator"`, `"admin"`.
- `moderators_see_ids`: moderators see players' whole 64-character IDs in Discord. Off, they read the first 8.
- `timeout_suspends`: someone timed out in Discord can't use their rank meanwhile. On unless you change it.
- A role that counts on only some servers has a block of its own:

  ```toml
  [[ranks.scoped]]
  role = "@Server 1 Mods"
  rank = "moderator"
  servers = ["Server 1"]
  ```

  On those servers it's that rank. A look (`/whois`, `/server`) only needs the rank somewhere, but what reaches every server takes the rank they hold on all of them: `/ban`, `/unban`, `/banall`, `/promote`, `/demote`, `/admin`, the word and name lists, `/clearstrikes`, `/confirm`, `/deny` and a kick with a time. Their ban belongs in that server's console: a `ban` line typed for one server stays that server's.

## 6. Choose where each message goes
Every kind of message the bot posts has its own line in `[messages]` in `discord.toml`:

```toml
votes-called = { channel = "logs", colour = "sky", post = true, ping = "" }
```

- **`channel`:** a name from the table above, or a channel itself (`"#my-channel"`).
- **`colour`:** a name from `[colours]`, or `"#RRGGBB"`. Change the colours there, or add your own.
- **`post = false`:** that one isn't posted at all.
- **`ping`:** who to ping when it's posted: a role (`"@On Call"`), or several (`["@Moderators", "@Admins"]`). **Who gets told** in `/huragok setup` sets the ones that matter most; any line's own panel in **Messages** sets its own.
- **`silent = false`:** it makes a sound for those getting notifications from that channel, ping or no ping. Reports, calls, appeals and Contact have it, as someone's waiting on them, and alerts do through `silent = false` in `[feed.alerts]`.
- **`remove_after_minutes`:** minutes until a post goes, for what's only true for a while. Seeding has it, for instance. It's for posts in a channel: a card or a webhook's post stays.
- **`stale_after_hours`:** hours until a ticket nobody's touched finishes itself.
- **`closes_to`:** where a ticket goes when it's finished, over `when_closed` in `[tickets]`: a channel, or `stay` or `thread`.
- **`as`:** `ticket`, `log` or `post`: what it is, whichever channel it's sent to. A kind of message sent to a channel of its own stays what it was unless this says otherwise. What wants an answer (a report, an appeal, a request, a call, Contact, `/ticket`) is a ticket whatever this says.

`/huragok gallery` shows you every message in its colour, saying where it goes, so you can see them all at once. Only you see it. `/huragok setup` → **Messages** changes a line's channel, colour, ping, what happens when it's finished, and whether it posts. **J**, **E** does all but the ping. The rest are for the file.
A feed's own `enabled` switches its whole group, and that's what **Features** lists.

## 7. The servers at a glance
Each of these is a feature of its own, off until you switch it on (`/huragok features`, or **J**, **F**). Their settings are the `[status.*]` and `[notify]` blocks in `discord.toml`:

| Feature | What it does | What it needs |
| --- | --- | --- |
| **Status board** | a card for each server, edited in place: online or not, map, mode, players, who's on, a Join button | the `status` channel |
| **Seeding pings** | asks people to come and fill a server that has a few players on | the `info` channel, and an opt-in role picked on **Who gets told** |
| **Alerts members pick for themselves** | `/notify`, below | the `info` channel |
| **The bot's status line** | "Watching 12 Spartans on 3 servers" (Discord adds the "Watching"), rotating; its dot goes idle when a server is down | nothing more |
| **The bot's bio** | its About Me in your Discord: servers and players | nothing more |
| **The bot's nickname** | "Huragok · 87 on" | Change Nickname |
| **Channel names and topics** | a channel named `🟢 Server 1: 12/16`; Discord allows only 2 changes in 10 minutes, so never more than one in 5 | Manage Channels |
| **Voice channel status** | the line under a voice channel | Set Voice Channel Status, and Manage Channels |

A card on the board reads **Online**, **Full**, **Restarting**, **Offline** (it should be up, and isn't) or **Stopped** (someone stopped it, and nothing's wrong). Through a restart's countdown it stays **Online**, with its Join button and how long is left.

**`/notify`** lets any member ask to be pinged when a server reaches so many players or a game type starts, where `notify-told` posts: `/notify players`, `/notify game`, `/notify list`, `/notify stop`. Five at most, per person, and each at most once in `cooldown_minutes`.

## 8. Check it works
11. `/huragok check` lists what's wrong first, each with what to fix, then what's fine.
12. `/huragok test` in a channel posts three messages, as the Superintendent, Vergil and
    Huragok, which go by themselves after a few minutes. In the window, **J** then **T** asks
    which channel and posts the same three, which stay there.
13. Have someone who isn't an owner try `/huragok setup`. They should be told no.

## What's what
| In Discord | |
| --- | --- |
| `/huragok claim <code>` | become an owner, with the window's code |
| `/huragok setup` | what's left to do, and everything else |
| `/huragok gallery` | every message, in its colour, with where it goes, for you alone |
| `/huragok features` | every feature's state, with its switch |
| `/huragok check` | everything the features that are on need |
| `/huragok test` | a test post here, as the Superintendent, Vergil and Huragok. They go by themselves after a few minutes |
| `/huragok reload` | read `discord.toml` again now, and say what's wrong with it |

Everyone sees `/huragok` in the list, but only owners can use it (`claim` is for anyone
with a code), and only the person using it sees the answers.

**In-game commands as slash commands,** once the `commands` feature is on: `/mute`, `/kick`, `/whois`, `/bans`, `/restartserver` and the rest.

- **A box for each thing it takes,** with players suggested as you type: only who it could do anything to, saying how they stand. `/unban` lists who's banned, `/letback` who's kicked or banned, `/unmute` who's muted, `/allowvote` who's lost their votes, `/clearstrikes` who has strikes and `/demote` who has a rank; `/ban`, `/mute` and `/novote` leave out who it already stands for; `/warn`, `/team` and `/respawn` only offer who's in a game. Each reads under the name they were last seen with, with an old name if that's what you typed. That's only what's offered: anyone can still be typed in, by name or by their whole player ID, as someone who's forgotten an old name may only have that. What you write in one box can't be read as another, so a reason starting "7d" is only a reason. The word list's commands have a box for each part of a line, in any order: `/banword` takes `words`, `mutes`, `kicks`, `bans` and `listname`, `/changebanword` a `listname` and its new steps, `/unbanword` `words` or a `listname` (alone, the whole list, after asking). `listname` suggests the lists, and `/unbanname` the banned names. `/respawn` only asks who.
- **Exactly the game's command:** the same rank from `commands.toml`, the same rule that nobody acts on their own rank or above, and a probationary moderator's are requests. It runs as `!kick` would, so what reaches every server still does. Someone who's left can be kicked with a time, picked from the list as you type.
- **Which server:** if none's named, a command about each server answers for them all, and one that changes something goes where the player is, or asks rather than guessing.
- **Answers** are only the asker's unless `public` in `[commands]` lists the command; those go from the channel after a while. Anything in `confirm` under `[console]` asks first here too.
- **Who's shown what:** the list is long, so staff's commands only show to members with the Discord permission in `staff_permission` (Manage Messages unless you change it, on the **Commands** panel). So do `/note`, `/flag`, `/ticket`, `/case`, `/server` and `/tickets`. `/huragok check` says if a role you've given a rank wouldn't see them. Showing a command gives nobody the right to use it: your rank decides.
- **The word list and the banned names,** `/bannedwords` and `/bannednames`, come whole however long they are: every list, every word and what each strike does, every name and the names the staff allowed, over as many messages as it takes.
- **Names:** Discord calls some of them something else, where the game's name would mislead: `/bannedwords` (`!rules`), `/bannednames` (`!bannames`), `/letback` (`!pardon`), `/clearstrikes` (`!pardonplayer`), `/reloadserver` (`!reload`). Their answers use these names too. `names` in `[commands]` renames any, and `hidden` leaves any out.
- **What isn't one:** `!calladmin` and `!strikes` mean you, in a game, so they're for the game. `!report` is `/report`, `!reports` is `/tickets`, and `/help` lists what your rank can use.
- **Players read** `Bravo was kicked by Mod`: your name in this Discord, or whatever `shown_as` says, which can be nobody at all.

| In the window: J, then | |
| --- | --- |
| **M** | mode and bot token |
| **W** | which Discord |
| **R** | ranks and owners |
| **N** | channels |
| **F** | features |
| **E** | every message: where it goes, its colour, on or off |
| **I** | tickets: what happens to a finished card, and who can close one |
| **Y** | the typed console: where lines are typed, who can, how answers come back |
| **P** | reports, appeals, Contact staff: how people reach the staff |
| **C** | check |
| **T** | test post |
| **K** | claim code |
| **V** | invite link |
| **Z** | start over: forgets this Discord, its owners, channels, ranks and switches, and every card; keeps the token and what staff wrote about players. The old file is kept beside the new one, as `discord.before-start-over-…toml`, token and all |
| **L** | the start loop's webhook (below) |
| **X** | the texts the bot posts, which `[text]` in `discord.toml` can reword (`"ticket.closed" = "words"`) |
| **O** | open `discord.toml` |

A **feature** is **off**, **waiting** (switched on but missing
something, and it says what) or **on**. `voice = "plain"` in `discord.toml` drops the
Superintendent, Vergil and Huragok for plain words and the bot's own name.

**Words.**

- **Ticket:** one incident and its card, with buttons. Its number is in the footer. What's done on it updates it, and it becomes the record: no more cards are posted about it. A **Now** field says where the player is and how they stand, while it's live.
- **Case:** one action on a player's record. A ticket can hold several. `/case` takes this number, which is on a card's lines and in `!whois`.
- **Log channels:** `logs` and `huragok-logs`, where finished tickets go. Their buttons still work, and pressing one brings the ticket back.

**What the buttons do.** Pressing one says nothing unless it failed or the card moved: the card's the answer. What your rank can't do is refused at the press, before any form; a time over your limit is refused when you send the form. If someone else acted on that player in the last minute, it says who and does nothing: press again if you still mean it.

| Button | Shows on | What it does | What the player reads |
| --- | --- | --- | --- |
| **Mute** | a player's ticket; greyed out if they're banned and gone | asks how long (needed) and why. Nobody hears or reads them. Someone who's left is muted as they next join. It doesn't count towards their strikes: **Strike** does | everyone: `Bravo was muted by <you>`, and they read why |
| **Unmute** | instead of Mute while muted | at once | their mute ends |
| **Kick** | a player's ticket; greyed out if they're banned and gone | asks how long and why. Empty: the server's own 10, 30, 60, then 120 minutes, and they must be on. A time: off every server that long (a moderator's is 1 day at most, `moderators_kick_at_most` in `[automod]`). `perm` is a ban | everyone: `was kicked by <you>`. With a time the server says they were banned, as it's the server's timed ban underneath. At the door: how long is left, and why |
| **Let back** | instead of Kick while they're kept out, or beside it if they're on another server | at once: they can join every server. A moderator can only lift what a moderator could have given | nothing: they find out by joining |
| **Ban** | a player's ticket | asks why (needed). For good, every server: ID, address, device | everyone: `was banned by <you>`. At the door: why, and how to appeal if you set that |
| **Unban** | instead of Ban | at once, every server. Another server's own `/ban` stays | nothing |
| **Ban everywhere** | someone banned on another server joining | puts that ban on every server | as for a ban |
| **Strike** | a report, or wherever `buttons` says | asks which rule and why. It goes on the word list's ladder, for what the word list can't hear, like voice | as for what the ladder does next |
| **Pardon strike** | a strike from Vergil | asks why. Clears every strike on that rule, or every rule if the card has strikes on several, and lifts what they did: unmuting alone leaves it counting | nothing |
| **Warn** | a report, while they're on | asks why (needed) | a private warning |
| **Whois** | a player's ticket | their record, for you alone | nothing |
| **Said** | wherever `buttons` says | their last lines of chat, for you alone | nothing |
| **Add note** | a player's ticket, if `notes` is on | a note for staff | nothing |
| **Discuss** | any ticket, while `threads` in `[tickets]` is on | opens a thread under it, or takes you there. On a Contact ticket it adds you to their private thread | nothing |
| **Server** | a server's alert | its state, and power buttons if your rank has them, for you alone | nothing |
| **Acknowledge** | Vergil's and the Superintendent's | nothing more's needed: to `huragok-logs`. While the server it's about is still down, it says you're on it and the card stays | nothing |
| **Close** | people's | done: to `logs`. On an appeal: nothing more's wanted, and it doesn't count against them for the wait. In a Contact thread: closes it and keeps a transcript | nothing, but an appeal's told it's closed |
| **Dismiss** | a report nobody's acted on | nothing to it: to `logs`. It doesn't count for those who reported | nothing |
| **Lift it** | an appeal | lifts it, and needs the rank that takes. They're told by DM. If it stands on another server too, the card says so and they aren't told yet: press it again | nothing in game |
| **Shorten it** | an appeal | asks how long from now. Lifts it, then gives a kick or mute that long. It won't make it longer, or go over what your rank can give. They're told by DM | nothing, then the door as for a kick |
| **Deny** | an appeal | asks why (needed, if they're being told), and whether they can appeal it again: after the usual wait, a longer or shorter one, or no more appeals. It's for whoever appealed, for that player: another Discord account can still appeal for them, as anyone can type a name, unless the appeal came with the player's code (with `code_only`, every one does). Something new against them can always be appealed. They're told by DM | nothing |
| **Let them appeal again** | a denied appeal's card, with its wait still going | they can appeal straight away, and they're told | nothing |
| **Confirm** | a request | it happens now, as yours | as for that action |
| **Deny** | a request | asks why. They're told | nothing |
| **Who's this about?** | a call for an admin, with players on | names the player, and their buttons appear. It stays, in case the first pick was wrong | nothing |
| **Contact the staff** | a channel everyone reads | opens a private thread with staff | |
| **Join** | the status board | opens the server's join page | |

Which of Mute, Kick, Ban, Strike, Warn, Whois, Said and Add note a card has is `buttons` in `[tickets]`, and for a report `buttons` in `[reports]`: the file's job. An appeal or a request has its own answers in place of Mute, Kick, Ban, Strike and Warn. "Every server" here and below holds with `across_servers` in `[automod]` on, as it is unless you change it. Off, what's done stays on the server it's done on.

A probationary moderator's press is a request, as their command is: it's written on the card, with a card of its own for someone to confirm. How it goes is written on the card they asked from, and they're told by DM. They can look and write notes freely. Scoped ranks count on the card's own server, as above.

**Staff tools.** Switch these on in **F**, and choose where each goes in **E**:

- **Where a ticket goes.** **Acknowledge** says what's been done is enough, and sends Vergil's or the Superintendent's to `huragok-logs`. **Close** sends people's to `logs`. Those nobody touches finish by themselves: Vergil's and the Superintendent's after `stale_after_days`, and any kind after `stale_after_hours` on its line in `[messages]` (a watch card after 12 hours, a call after a day, a report after 2 weeks). A report someone's acted on finishes after `acted_after_hours` in `[reports]`, a day unless you change it. `stale_after_days = 0` means nothing finishes itself, whatever a line says. Requests, appeals and Contact wait for someone, as they need a decision. So a channel of tickets holds what needs acting on. A card in a log channel keeps its buttons: acting on it sends it back, to be acknowledged again. So a moderator's own action in game goes straight to `logs` as a card, and can still be acted on from there.
- **What happens to a finished card is yours to choose:** `when_closed` in `[tickets]` (`/huragok setup` → Tickets, or **J**, **I**). `move` is the default: to its log channel. `stay` leaves it where it is, in the closed colour. `thread` also gives each card a thread of its own from the start, which closing locks and hides (the bot needs Manage Threads). Log channels can be shared, by giving `logs` and `huragok-logs` the same channel, or a kind of card can have its own: `closes_to` on its line in `[messages]` (setup → Messages), which also takes `stay` or `thread` for that kind alone. Whatever goes with a card (a clip someone reported with, a Contact thread's transcript) goes where the card will end up, so it isn't left behind.
- **What the bot says only to you goes by itself:** a word about what happened after 10 minutes, and a command's answer or something you asked to read after 14, the most Discord allows (`[answers]`). What everyone can see is only there if you've asked for it.
- **`/server [name]`** (with the `commands` feature) shows a server's state, seen only by you, for anyone with a staff rank. Those with `power_rank` (admins and above, unless you change it) also get its power buttons: Start, Stop, Restart, Restart after the game, Restart when empty, Cancel, Check for updates. Each asks first, with a **Yes, and say why** that everyone on will read, and goes in the audit log, and a press from an alert goes on that alert; Check for updates only asks GitHub, so it does neither. `power_buttons` says which there are (the **Commands** panel). **Refresh** looks again. A ticket in `alerts` has a **Server** button that opens it. **`/status`** lists the servers, for anyone.
- **The typed console** (`console`, in `/huragok setup` → Console) lets staff type in a console channel. A line starting with `!` is one of Huragok's commands, as your rank allows in game, and on probation it's a request. Anything else is a console line (`players`, `say hello`), with the same checks as the relay: `raw_rank` says who can (`moderator`, `admin`, `owner`, or `off` for nobody; admins and above unless you change it), and nobody acts on someone of their own rank or above.
  - **Answers** come back as a reply, or in a thread; long ones as a file, or in pieces. Everyone with a rank reads this channel, so answers never show an address, or a whole ID unless `moderators_see_ids` is on, whoever asked. Lines in `confirm` ask "Send `line` to **server**?" first.
  - **Everyone else's lines** are ignored, or deleted if you choose. A line starting with `ignore_prefix` (`//`, say) is ignored from anyone, so staff can talk there. `audit` says which lines go in the audit log.
  - **A password** typed there is deleted at once, if the bot has Manage Messages.
  - **It clears itself:** once nothing's been said or pressed for `clear_after_minutes` (10, unless you change it on the Console panel, and at least 5), what was there goes to the logs as a file and the channel's emptied, all but the menu and anything pinned, ready to use again; the server each person picked is forgotten too (`forget_server`). What was in the channel before the console first looked at it is never touched, and nothing's removed unless the file got there. Without **Manage Messages** in that channel, or if it's also used for something else, nothing happens at all, and `/huragok check` says so.
  - **It needs Message Content Intent** in the developer portal (the **Bot** page, under **Privileged Gateway Intents**, then **Save Changes**). The bot notices the switch by itself within a few minutes, and signs in again; no new invite for that. It asks for messages only while the console, or Contact's transcripts, are on.
  - **One channel:** with several servers, a menu pinned at the top says which server your lines go to (`remember_server` says whether that's each person's or everyone's; the bot needs Pin Messages to pin it), or start a line with the server's name: `server1: players`. **A channel each:** `layout = "per-server"`, and give each server its channel.
  - **Keep them private:** the answers can name anyone. It doesn't run until they are, and says so to anyone with a rank who types there, unless you allow it in `/huragok setup` → Console (`public = true` in `[console]`).
- **Reports** (`reports`) let anyone tell staff about a player, with `!report <player> [why]` in game, `/report` in Discord (with a clip or a screenshot if they have one), or **Report message** on a right-click (`right_click`). Each is a ticket in `reports` with what the player said, and who reported them with how their reports have gone. It's one card for a player while it's open: anyone else's report is added to it. It makes a sound, and moderators and up on the servers are told in game (`tell_in_game`), as they're the only staff who can hear what's being reported. One person can have `most_at_once` of them open at a time, and report again only after `reporter_cooldown_seconds`.
  - **Enough different people** within a while pings staff (`threshold`, and who's pinged is on **Who gets told**): that's written on the card, and the ping itself goes after an hour.
  - **Acting on it** counts for those who reported, and they're told, in game or as they next join, or by DM if they reported from Discord. `tell_reporter` says when: `acted`, `all` (a dismissal too) or `no`. The player isn't told who reported them. A moderator's action or strike on that player, typed in game or from a card, goes on the report's card, and counts. **Dismiss** closes one with nothing to it.
- **Appeals** (`appeals`): someone banned types `/appeal` with their name in game, their player ID, or the code from their ban's message, and answers your `questions`. It's a ticket in the `appeals` channel, or `reports` until you set one, with the case and what they said before it, and **Lift it**, **Shorten it**, **Deny** and **Close**. They're told how it went by DM, if they take DMs from your Discord; the card says if they couldn't be told. Shortening a ban makes it a kick with a time. Lifting an appeal against what Vergil did takes that strike off too (`lift_clears_strike`). `for` says what can be appealed (kicks and mutes too, if you like), `cooldown_days` how soon they can ask again, and `in_reason` adds a line to a ban's message in game, such as `Appeal on discord.gg/yours: /appeal {code}`.
  - **The code** is random, and stays the same for that player. With `{code}` in `in_reason` the card says whether they came with it or with only a name, since a name is anyone's to type. `code_only = true` refuses a name alone; a `/ban` in game has no code, so those people need Contact staff. Only an appeal with the code counts against the player for the wait, so a stranger typing their name can't lock them out.
  - **Nothing tells them** they can appeal unless `in_reason` does: `/huragok check` says so when it's empty.
- **Contact staff** (`contact`): a button in a channel everyone can read (`channel`, `info` unless you say) opens a private thread with the staff, for what doesn't fit a form. It's seen by them, by the role in `thread_role`, and by anyone who presses **Discuss** on its ticket in `reports` (those with Manage Threads can always see them). One open at a time for each person, and another only after `cooldown_minutes`. Closing it keeps a transcript (`transcript`, which needs Message Content Intent: without it the card says there's none) and locks the thread; `close_after_days` closes quiet ones. In that channel, people need **Send Messages in Threads** to write back, even where they can't type in the channel itself. The bot needs **Create Private Threads**, and **Manage Threads** so that a closed thread can be opened again when its card comes back; that also lets the bot see every private thread in your Discord.
- With the `commands` feature, **`/status [server]`** is for anyone, and **`/case <number>`** shows staff a case by its number on a card or in `!whois`, which isn't the ticket's number. **`/tickets`** lists what's live, for staff alone: what someone's waiting on first, then what's wrong, then the rest, each with what was last done; if it's long, it's a file. Staff can drop clips and screenshots into a card's thread: they stay with it.
- **`/ticket <player>`** (with the `commands` feature, for those who can close a ticket) posts a card with the buttons for anyone, any time.
- **A probationary moderator's request** (`feed.requests`) is a ticket in `reports` with **Confirm** and **Deny**, however they asked, in game or with a slash command. Either decision, here or typed in game, is written on the card and closes it to `logs`.
- **Vergil's strikes** (`feed.vergil`) are one live card per player, in `staff`: a later strike adds a line, and **Acknowledge** clears the lot. A strike on someone with an open report goes on the report's card too.
- **A word from a list that only reports** (`vergil-heard`) is a line on that same card, or a card of its own, with what they said: nothing was done in game, so it's the staff's to decide, with the usual buttons. **A name with one of those words** (`vergil-name-asked`) is a card with two buttons, **Ban the name** and **Allow name**, for whoever may use `/banname`: either closes it. See [automod.md](automod.md#lists-that-only-report).
- **Problems with a server** are one ticket in `alerts` each, added to while it lasts and closed when it's over: a crash when the server's back, a slow server when the watchdog says it's healthy, a failed update when one works. A stop by someone closes it too. A FAULTED server's card says when it tries again. So `alerts` holds what's wrong now, but for two things: nothing says a relay lockout or a part switched off is over, so those need **Acknowledge**, and anything left untouched finishes after `stale_after_days`.
- **What has nothing to act on** (votes, joins, servers starting) is one line in a log channel.
- **The colours** say where a card stands: its own message's colour until someone touches it, then `active`, `stale` or `closed` from `[tickets] colours`.
- **The watch list** (`feed.watch`) tells staff about someone back soon after a kick (within
  `back_after_kick_minutes` of the server's block ending, unless they were pardoned), a name
  made to look like a moderator's or an owner's (`0livia` for Olivia), someone new on a nearly empty
  server, someone banned on another of your servers, and the server's anti-cheat acting.
  Each is its own line under `[messages]`, so any of them can go elsewhere or be turned off.
- **Notes and flags** (`notes`) are what staff write about a player. `/note add` (`days` says when it's dropped), `/note list`
  and `/note remove`; `/flag set`, `/flag list` and `/flag clear`. They show on `!whois` and on tickets. With the
  watch list on too, anyone flagged with a word in `watch_flags` tells staff when they join, in the flag's colour.
  `flags` in `[notes]` is your own list of words and colours. Probationary moderators can read
  and write notes; taking one away and flags need a moderator.
- **`!calladmin [why]`** (`feed.calls`) lets a player call staff from the game, with the last
  of the server's chat on the card, once per `cooldown_seconds`. `words` lists chat words that
  call too. Moderators and up on the servers are told in game (`tell_in_game` in `[reports]`). Say who's on duty on **Who gets told**. **`!mods`** says
  which staff are in a game now; staff who are only on Discord aren't counted.
- **In the game,** these are only there while Discord can take them: `!calladmin` with `feed.calls` on, `!report` with `reports` on and the full bot, `!mods` with the mode anything but off. Otherwise it's "Unknown command", and `!help` doesn't list them. While they are, quoting a word from the word list in `!report` or `!calladmin` isn't struck: whoever's reporting it is helping. `[off]` in `commands.toml` switches any of them off. **`!reports`** lists for moderators what's open, reports and calls on every server, while the full bot takes either.
- **Seeding** (`status.seeding`) asks people to come and fill a server that has a few players
  on, once per `cooldown_minutes` a server. The post goes after half an hour, or when the server goes down. Make an opt-in role, and pick it on **Who gets told**.

**A few settings have no panel,** and are worth knowing about. Each has its note in the file:

| Setting | What it does |
| --- | --- |
| `[feeds] servers` | which servers the feeds are about: `"*"`, or a list of names |
| `[feeds] style` | `card` or `compact` (one line), for what's a post, not a ticket |
| `[feeds] also` | webhook addresses to post copies to, a partner's Discord say. Copies have no buttons |
| `[feeds] merge_seconds` | a burst of one kind of post becomes one when it's over |
| `[feed.moderation] sources` | which actions it shows: `!`, `/`, `vergil`, `console`, `relay`, `window`, `discord`, `vote`, or `*` |
| `[feed.<name>] webhook` | post that feed through a webhook. In bot mode too: its posts are then plain ones, with no buttons |
| `[personas]` | the three names and their pictures. `enabled = false` drops them, as `voice = "plain"` does |
| `[tickets] evidence_lines`, `evidence_minutes` | how many of a player's chat lines a card shows, and how far back they can be. It's read as what they said just now |
| `[answers]` | how long what the bot says stays |
| `[limits]` | floors on the timers. `logins_per_day` is the one to know about |

**When Huragok is down, so is the bot.** The start loop can still say so: make a webhook in
a staff channel (below), and give its address to **J**, **L**. It's posted when Huragok stops
unexpectedly, unless the mode is off.

## Webhook only: no bot
A webhook can only post: no commands, buttons or roles. It suits a host who only wants the
feed, or who can't add a bot to the Discord. Feeds, the status board and seeding can post
this way; the rest needs the bot.

1. In Discord, a channel's settings → **Integrations** → **Webhooks** → **New Webhook** →
   **Copy Webhook URL**. Treat that address as a password too.
2. **J**, **M**, **2**.
3. **J**, **T** and paste the address: three test posts.

Features that can post through a webhook each take their own address (`webhook = "..."` in
their block of `discord.toml`), and **J**, **F** asks for it as you switch one on.

## When something goes wrong
**Start with `/huragok check`, or J then C:** most answers are there. The window's red lines
say why the bot is off, and `logs/huragok.log` has the details. The token never goes in them.

| What you see | What it means | What to do |
| --- | --- | --- |
| "Private application cannot have a default authorization link" | an install link is set | **Installation** page: **Install Link** to **None**, save, then switch Public Bot off |
| "Discord refused the token" | it's wrong, or it was reset | **Reset Token**, then **J**, **M**, **3** |
| Paste does nothing at the token prompt | your console doesn't paste there | put it in `discord.toml` (section 2) |
| The invite says a code grant is required | **Requires OAuth2 Code Grant** is on | switch it off on the **Bot** page |
| Your Discord isn't in the invite's list | you can't manage that Discord | use an account with **Manage Server** there |
| `/huragok` doesn't come up | Discord hasn't caught up, or the bot isn't connected | Ctrl+R; then look at the top of **J** |
| Old cards stay up, and a second one appears when they change, after removing the bot and adding it back | Discord deleted the webhook that posted them, so nothing can edit them now | delete the old ones by hand, or give the bot Manage Messages. New cards work as usual |
| The bot's dot is yellow | a server that's meant to be up isn't. A server you stopped doesn't count, unless they all are, which is yellow too. Red: none are up | look at the status board. `dot = "online"` in `[status.presence]` keeps it green |
| "the bot isn't allowed to add its commands" | it was added without `applications.commands` | open the invite link again: it tries again by itself |
| Moderators don't see `/mute` | they lack the Discord permission in `staff_permission`. An owner or a Discord admin sees everything, so you won't notice | **Commands** panel, or give their role that permission. `/huragok check` names the roles |
| A card turned up, and nobody's phone buzzed | nothing pings until you say who | **Who gets told** in `/huragok setup` |
| Players get "Unknown command" for `!report` or `!calladmin` | Discord can't take it: the mode's off (for `!report`, anything but the full bot), or the `reports` or `feed.calls` feature is, or `from` in `[reports]` leaves out `game` | switch it on |
| The console isn't clearing itself | the bot lacks Manage Messages there, the channel's also used for something else, or there's nowhere to keep what was said | `/huragok check` says which |
| You've wiped your test Discord, or moved to another | it's still set up for the old one | **J**, **Z** starts over; **J**, **W** only changes which Discord |
| "The application did not respond" | Huragok isn't running or was restarting | check the window; type the command again |
| "This panel has closed" | it was open over 10 minutes, or the bot signed in again meanwhile | type the command again |
| "This bot belongs to another Discord" | it's set up for a different one | **J**, **W** |
| "Only the owners…" | you aren't one yet | **J**, **K**, then `/huragok claim` |
| "That code isn't right" | it was used, ran out, or you've had too many wrong tries | **J**, **K** for a new one |
| Test posts don't appear | the bot can't see or post in that channel | add its role to the channel; `/huragok check` names what's missing |
| Posts come from the bot's name | no **Manage Webhooks** there, the channel has Discord's 15 webhooks already, or `voice = "plain"` | it still works; give it the permission if you want the three names |
| Webhooks refused in a Discord that requires 2FA for moderators | Discord wants two-factor sign-in on the account that owns the bot | turn 2FA on for that account |
| "the bot signed in N times today" | something keeps restarting it, and Discord resets a token at 1,000 logins a day | find what's restarting it; the bot comes back tomorrow, or raise `logins_per_day` |
| Two answers, or "already acknowledged" errors | two Huragoks on one token | one bot for each Huragok |
| "discord.py isn't installed" | an update didn't finish installing it | start Huragok with `start.cmd` or `./start.sh` again |

The dashboard says in red what needs you: a token refused, a channel Discord refused, the bot no longer in its Discord, signing in or Discord unreachable for over 2 minutes, and anything switched on that's waiting.

A mistake in `discord.toml` never stops Huragok or your servers. A setting it doesn't know
is a warning, and one with an older name says what it's called now. If the file can't be read at all, the bot stays off at the start and says why;
if that happens while it's running, what was in force stays until the file is right again.
