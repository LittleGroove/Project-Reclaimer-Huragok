# Managing a running server

Setting a server up is in [setup.md](setup.md). This is what the window and the
commands do with a server that's running: its console, its settings changed live,
bans and admins, updates of the server program, and a console for moderators who
aren't at this PC.

## The console: A
**A** picks a server and opens its console through the supervisor's own RCON
connection (the spot the server keeps for its own machine, so moderators' 4 spots
stay free). The first time, it asks your name: the server records it and players
read "kicked by Alice" instead of "by an admin". While the console is open the
server's events scroll in as they happen: chat lines, joins and leaves, kicks,
bans, mutes, votes, the game's phases, reload results. Type any console command
(`help` lists them; `players`, `say`, `kick #3 camping`, `mute Bravo 30m spam`,
`map Guardian`, `nextmap`, `startvote playlist`, `reload`…).

From outside: `start.cmd rcon [-s server] [--by Alice] <command>`.

## Settings changed live: D
**D** lists a server's settings with their current values and changes one at a
time, writing `dedicated.toml` in place (its comments stay). Since Project
Reclaimer 0.9.18 the server re-reads the file a couple of seconds later, so no
restart is needed for most of them. The screen says, for each, when it applies:

| Applies | Settings |
| --- | --- |
| at once | join password, max/min players, max ping, votes, voice (global, team, proximity), text chat, welcome lines, mod sharing, host name, the folder's admins |
| from the next game | friendly fire, team changing |
| from the next vote | the playlist |
| the server restarts by itself once empty | hidden, port, anti-cheat, VPN block, chat log, load timeout, character mods, enabled, stats server, UPnP, the console password and address |
| needs this server restarted from here (R) | public address, the discovery master |

The dashboard's events show the server's answer: `settings reloaded: max_players
16 → 12`, or `settings NOT reloaded, nothing changed` with what's wrong. Three
things change live over the console instead of the file, until a restart:
`servername`, `password` (join) and `maxping`.

## Bans, admins and the VPN block: B
**B** on a running server: the ban list (`bans`), `unban`, a ban of someone who
isn't connected (player ID, address or range, with a time and a reason), `pardon`
(let a kicked player back in now), the admins (`admin list`, `admin add`, `admin
remove`: on every server sharing the ban list, no restart), and the VPN block
(`vpn`, `vpn <address>`, `vpnallow`, `vpnrevoke`). Everything goes through the
console with your name on it. On a stopped server it shows the ban list from its
folder (`dedicated bans`).

## Updates: U
**U** offers two things:

1. **The supervisor** itself, from its GitHub releases: the servers keep running
   while it updates.
2. **The server program** (Project Reclaimer), from its releases. Players must be
   on exactly the server's version, and releases have come out daily, so a server
   left behind empties out. The server's own `auto_update` would restart every
   server the moment a release downloads, mid-game; the supervisor turns it off and
   does this instead:
   - every few hours it looks at the releases and compares with the version each
     running server reports; the dashboard header says `server program 0.9.18
     (0.9.19 out: U)`;
   - when you say so (**U**, or `start.cmd update --program [--now]`), it downloads
     `project-reclaimer-dedicated-v<version>.exe`, checks it against the release's
     `SHA256SUMS.txt`, puts it in place of **every** copy in the server folders and
     at the root (the old ones go to `records/previous-program/`), and restarts each
     running server **after its game** (with the usual chat warnings), or now;
   - `[server_updates] install = "after_game"` in `settings.toml` does it by
     itself; `"now"` restarts at once.

## A console for moderators elsewhere: the relay
Moderators who aren't at this PC normally use the official RCON client against
the server's own console: one shared password, one of the 4 spots each, and five
wrong passwords lock an address out of the server.

With `[relay] enabled = true` the supervisor listens itself, at each server's
game port plus `port_offset` (49176 → 50176), speaking the server's own WebSocket
console protocol. So the **unchanged official RCON client** (host `127.0.0.1`
through an SSH tunnel or VPN, port 50176) and any tool that speaks that protocol
connect to the supervisor instead, and:

- each moderator has **their own password** (`[[relay.user]]` with `name`,
  `password`, `role`): the password is what tells them apart, so no two alike;
- only the commands **their role allows** pass, from the same lists as
  `commands.toml` (a moderator can `mute` and `kick`, not `ban`; read-only
  commands such as `status`, `players`, `maps`, `bans` always pass; `admin`,
  `reload` and the join `password` need owner), and the reply says so otherwise;
- the server reads **who acted**: players see "kicked by Alice", the server log
  and the supervisor's log record it;
- events reach every signed-in moderator; players' addresses only reach admins;
- **one** upstream spot serves everyone, and a wrong password locks nobody out
  of the server (five in ten minutes lock that address out of the relay);
- the dashboard header shows who's signed in (`consoles: Alice (LG Test Server)`);
  removing a moderator is deleting their block, with no server restart.

It isn't encrypted (neither is the server's console): keep `address =
"127.0.0.1"` and give moderators an SSH tunnel (`ssh -L 50176:127.0.0.1:50176
you@host`) or a VPN.

**Source RCON tools** (the protocol of Valve's games, which many panels and
libraries speak) use the same passwords at the game port plus
`source_port_offset` (49176 → 51176; `0` switches it off). Sign in with the
moderator's password, send a command, read the text; a refused password answers
with id -1 as Source servers do, and the end-of-reply marker clients send is
echoed back. Source RCON has no events.

## What a server plays next
The dashboard's GAME column shows the game under way and, when one is set, the
next one (`playing Guardian Slayer → next Valhalla Slayer`, from a `nextmap`
override), or `(voting)` while a playlist ballot is open. A stopped server shows
what it's set to play (`plays playlist infection`).
