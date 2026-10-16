# How the supervisor is built

The supervisor is one program. Its core keeps the servers up and holds the
pieces everything shares (one RCON connection per server, the live state of each
server, the event bus, the records). On top of it run its **parts**:

| Part | Folder | Settings | |
| --- | --- | --- | --- |
| `commands` | [parts/commands](../engine/reclaimer_supervisor/parts/commands) | `commands.toml` | The in-game commands. Always on. |
| `automod` | [parts/automod](../engine/reclaimer_supervisor/parts/automod) | `[automod]`, `automod.txt` | The word list |
| `watchdog` | [parts/watchdog.py](../engine/reclaimer_supervisor/parts/watchdog.py) | `[watchdog]` | Restarts a struggling server at a good moment |
| `eventlog` | [parts/eventlog.py](../engine/reclaimer_supervisor/parts/eventlog.py) | `[eventlog]` | Every event as JSON |

Parts are a way to keep the code apart, not something hosts install: everything
ships together, and new work comes as a pull request. Every command shares one
set of names, and [tests/test_parts.py](../engine/tests/test_parts.py) fails if
two parts claim the same one.

## Adding a command
Most new things are commands. Put one in the file of
[parts/commands](../engine/reclaimer_supervisor/parts/commands) it fits
(`players.py` for looking people up, `moderation.py` for acting on them,
`server.py` for running the server), or start a new file for a new group and add
its class to `Commands` in `parts/commands/__init__.py`.

```python
# parts/commands/players.py
@command("seen", usage="<name>", help="When a player was last here", role="moderator")
async def seen(self, call: CommandCall) -> None:
    found = self.ctx.players.find(call.text)
    if not found:
        await call.reply(f"Nobody called '{call.text}' has been seen.")
        return
    when = time.strftime("%Y-%m-%d", time.gmtime(found[0].last_seen))
    await call.reply(f"{found[0].last_name} was last here on {when}.")
```

- `role` is only the default: hosts move commands between roles in
  `commands.toml`, and the file the first start writes lists every command with
  its `help` text, so give every command a `help` that says what it does.
- Commands are public chat: everyone in the channel sees them. Answer with
  `call.reply(text)`, which only the caller reads (long text is split into lines).
- `call.player`, `call.role`, `call.server`, `call.state`, `call.prefix`,
  `call.args` (split like a shell, so `"two words"` stay together) and `call.text`
  (the rest of the line as typed).
- `await call.find_player(ref)` resolves `#3`, a player ID, an exact name or the
  start of one, and tells the caller when it matches nobody or several. Names
  aren't unique (one real night had 30 players called "Player"), so prefer `#numbers`.
- `self._target(call, ref)` does the same and refuses players of the caller's
  role or above; use it for anything done *to* a player.
- `self._rcon(call, "command", *args)` passes a line to the server's console as
  the caller and shows them the answer; `self._line(call, "name")` passes the
  console command of the same name with the caller's words.
- Pick a name that says what the command does. `!yes` and `!no` are the server's
  votes; lines starting with `/` are the game's own.

## Adding a part
For something that isn't a command, such as watching events, running on a timer,
or keeping state of its own, write a part: a module (or a package, for a bigger
one) in [parts](../engine/reclaimer_supervisor/parts) with one `Part` subclass,
listed in `parts/__init__.py`.

```python
# parts/greeter.py
from ..chat import CommandCall
from ..events import Event
from ..part import Part, command, every, on


class Greeter(Part):
    name = "greeter"                       # its section in settings.toml: [greeter]
    description = "Welcomes players and answers !discord"
    acts_as = "Greeter"                    # players read "kicked by Greeter"; the host's `name` setting overrides
    defaults = {"invite": ""}              # settings the host may leave out
    enabled_by_default = False             # on unless [greeter] enabled = false, or the other way round

    def check_config(self) -> None:        # before setup; ValueError switches only this part off
        if not isinstance(self.config["invite"], str):
            raise ValueError("invite must be text")

    async def setup(self) -> None:         # once, before any event
        ...

    @on("rcon.join")
    async def welcome(self, event: Event) -> None:
        record = self.ctx.players.get(event["id"])
        if record and record.visits > 1:
            await self.ctx.tell(event.server, event["id"], f"Welcome back, {event['name']}!")

    @command("discord", help="Our Discord invite")
    async def discord(self, call: CommandCall) -> None:
        await call.reply(self.config["invite"] or "No Discord set.")

    @every(minutes=10)
    async def remind(self) -> None:
        ...

    async def teardown(self) -> None:      # once, when the supervisor stops
        ...
```

Then add its section to
[templates/settings.toml](../engine/reclaimer_supervisor/templates/settings.toml),
tests under `engine/tests/`, and a line in the README. `check` shows each part,
whether it's on and any problem with its settings; a part that reads its own
files can check them there too by defining `preflight(config, home)`, as automod
does with its word list.

## What `self.ctx` gives a part
| | |
| --- | --- |
| `ctx.servers` | Each server's live picture by name: `players` (by `#number`: `name`, `id`, `ip`, `admin`, `muted`, `team`), `phase`, `map`, `mode`, `max_players`, `admins`, `connected`, `status_age`, `rcon_latency_ms` |
| `ctx.server(name)` | One of those |
| `ctx.say(server, text)` | A `[Server]` line everyone reads |
| `ctx.tell(server, player, text)` | A `[Server]` line one player reads (a `Player` or a player ID) |
| `ctx.players.get(id)`, `.find(text)` | Everyone ever seen on this machine: names used, IPs, first and last seen, visits |
| `ctx.role_of(server, player)` | `player` (everyone), `moderator`, `admin` (the server's own admins) or `owner` |
| `ctx.moderation` | `warn`, `mute`, `unmute`, `kick`, `ban`, `unban`, `pardon`: recorded with a reason, shown to players as by your part (or the `by=` you pass), and dry-run aware. Use these for anything done to a player |
| `ctx.lifecycle` | `restart(server, when="now" / "after_game" / "when_empty", reason=…)`, `cancel(server)`, `can_control` |
| `ctx.rcon(server)` | The raw console: `await ctx.rcon(server).command("load", "Guardian", "Slayer")`, for what the rest doesn't cover |
| `ctx.db` | The shared SQLite database. `ctx.db.migrate([...])` creates your tables (prefix them with your part's name); `execute` and `query` run SQL |
| `ctx.config` | Your section with `defaults` filled in (also `self.config`) |
| `ctx.emit(name, server, **data)` | An event for the other parts, named `<your part>.<name>` |
| `ctx.log` | A logger tagged with your part's name (also `self.log`) |
| `ctx.home` | The supervisor's folder (settings.toml, commands.toml…), for files of your own |
| `ctx.dry_run` | Whether the host set `dry_run` for your part (or for everything) |

## Events
Subscribe with `@on("name")`, `@on("group.*")` or `@on("*")`. Each event has
`name`, `server` (its name, or `None`), `time` (Unix seconds) and `data`, which
you read as `event["field"]` or `event.get("field")`.

The `rcon.*` events are the server's own, with the fields it sends (from the
server's RCON.md, v0.9.18):

| Event | Fields | When |
| --- | --- | --- |
| `rcon.chat` | `channel` (`all`, `team`, `server`; proximity seen in logs), `team`, `name`, `id`, `text` | A line everyone in the channel read. Muted players' lines and private lines never arrive. |
| `rcon.join` | `number`, `name`, `id`, `ip`, `admin` | A player was admitted |
| `rcon.leave` | `number`, `name`, `id` | A player left or was removed |
| `rcon.refused` | `name`, `id`, `ip`, `reason` | A joining player was turned away |
| `rcon.kick` | `name`, `id`, `by`, `reason`, `seconds` | `by` is an admin, `RCON`, `Alice (RCON)`, `vote`, or `Server` (load timeout) |
| `rcon.ban` / `rcon.unban` | `name`, `id`, `ip`, `device`, `by`, `reason`, `expires` / `target`, `by` | |
| `rcon.mute` / `rcon.unmute` | `name`, `id`, `by`, `reason`, `expires` / `time` when it ran out | |
| `rcon.pardon`, `rcon.admin_added`, `rcon.admin_removed` | `target` or `id`, `name`, `by` | |
| `rcon.vote` | `state` (`called`, `passed`, `failed`, `cancelled`), `subject`, `caller` | Players' votes |
| `rcon.kill` | `killer`, `victim`, `report` | A death. **`killer` and `victim` are engine IDs, not `#numbers`**; how they map to players is still to be worked out on a live server |
| `rcon.phase` | `phase` (`no_game`, `playing`, `round_over`, `between_rounds`, `finished`), `map`, `mode` | The server can send `finished` for the next game just before it starts; check it matches the game that was `playing` |
| `rcon.control` | `action`, `ok`, `text` | Reloads (`action` = `reload`), queued game actions |
| `rcon.cheat` | `name`, `id`, `category`, `detail`, `action` | Rejected game data |
| `rcon.connected` / `rcon.disconnected` / `rcon.lagged` / `rcon.full` | | The supervisor's connection |

The supervisor adds its own:

| Event | Fields |
| --- | --- |
| `server.started`, `server.crashed` | from the built-in supervisor's console (backend `windows`): `pid`; `reason`, `kind` (`hang`, `menus`, `error`, `panic`, `engine_exit`, `other`) |
| `server.starting`, `server.stopping`, `server.stopped`, `server.all_stopped` | `expected` on `stopped` |
| `server.restart_planned`, `server.restarting`, `server.restarted`, `server.restart_failed` | `when`, `reason`, `by` |
| `server.hung`, `server.recovered` | `status_age` |
| `log.falling_behind` | `fps`, `behind`, `window`, `average`, `longest_ms`, `players` |
| `log.upload` | `rate`, `unit`, `players` |
| `log.roster_lost` | `lost`, `of`, `present`, `details` (the engine's JSON snapshot, as text) |
| `log.rcon_signin`, `log.rcon_refused`, `log.reload`, `log.online` | from `server.log` |
| `moderation.action` | `kind`, `target_id`, `target_name`, `by`, `source` (the part), `reason`, `duration`, `dry_run`, `ok`, `text` |
| `supervisor.started`, `supervisor.stopping`, `supervisor.part_disabled` | `parts` on `started`; `part`, `reason` on `part_disabled` |
| `automod.strike`, `automod.pardon` | `player_id`, `player_name`, `rule`, `strike`, `action`, `source` (`chat`, `name`, `manual`), `evidence`, `by`, `dry_run` |

Switch on `[eventlog]` to see every event as JSON while you work.

## Ground rules
- **Don't block.** Handlers are `async`; use `await asyncio.to_thread(...)` for slow
  work. Each handler has 10 seconds (`handler_timeout` in the part's section changes it).
- **Each part has its own queue.** Events reach it in order, one at a time, and a
  slow part never holds up the others or the RCON connection.
- **Failures are contained.** An exception is logged; five within ten minutes switch
  that part off (and only that one), with a `supervisor.part_disabled` event.
- **Everything done to a player goes through `ctx.moderation`,** so it's recorded
  with who and why, respects dry run, and shows players who acted.
- **Never resend a moderation command** after an error: it may have run already.
- **Never retry a refused RCON password:** five in ten minutes lock out every tool on the machine.
- **Test against the fake server** in `engine/tests/fake_rcon.py`;
  `engine/tests/test_supervisor.py` shows how to run the whole supervisor around a part.

## How it fits together
```text
 servers' RCON ──► one RCON client per server ─┐
 data/<server>/server.log, status.json ──► file watcher ─┼─► event bus ─► live server state
 `dedicated` console (windows backend) ──► backend ─┘          ├─► in-game commands ─┐
                                                               └─► each part's queue ◄┘
 keepalive (hung?) · lifecycle (restart now / after the game / when empty) · moderation (one ledger)
 commands.toml (reloaded when edited) · records/ (SQLite) · logs/
```

A server is only called hung when `status.json` (rewritten every 5 seconds) and
RCON have **both** gone quiet.

## Releasing
Hosts get the supervisor, and its updates, from GitHub releases. A release is a
tag plus a zip made from it with `git archive`, which leaves out what
`.gitattributes` marks `export-ignore` (the tests and the repository's own files):

```sh
# 1. Set __version__ in engine/reclaimer_supervisor/__init__.py, add the
#    changes to docs/changelog.md, commit and push.
# 2. Tag it, make the zip, publish:
git tag v0.2.0 && git push origin v0.2.0
git archive --format=zip --prefix=reclaimer-supervisor/ -o reclaimer-supervisor-0.2.0.zip v0.2.0
gh release create v0.2.0 reclaimer-supervisor-0.2.0.zip --title "Reclaimer Supervisor 0.2.0" --notes-file notes.md
```

Running supervisors find it within `check_hours` and offer it to their host. The
notes' first lines are what their window shows, so start with what changed for
hosts. An update replaces `engine/` (except `engine/.venv`), the start scripts,
`docs/`, `README.md` and `LICENSE`; never the host's own files.
