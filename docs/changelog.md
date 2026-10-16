# Changelog

## 0.1.0 (unreleased)
First version, tested against Project Reclaimer v0.9.18.

- **One folder you put in your server folder.** `start.cmd` (Windows) or
  `start.sh` (Linux) is the one way to start it: the first time it installs
  what it needs into `engine/.venv`, writes `settings.toml`, `commands.toml` and
  `automod.txt`, and picks how to run the servers from what's in the server
  folder. The program lives in `engine/`; updating means replacing it.
- **Keeping servers up:** backends `windows` (the supervisor runs each server),
  `docker`, `command` and `attach`; restarts now, after the game or when empty;
  hang detection. Closing the supervisor leaves the servers running;
  `stop` stops both. Only one supervisor runs from a folder at a time.
- **In-game commands with permissions:** `commands.toml` lists, for each role
  (`everyone`, `moderator`, `admin`, `owner`), its players and its commands, and
  `[off]` for commands nobody can use. Edits apply while running. Admins are the
  server's own, read live over RCON so the lists never drift apart. The
  commands: moderation (`!warn` `!mute` `!kick` `!ban`…), looking people up
  (`!players` `!whois`), and running the server (`!map` `!say` votes, teams…).
- **automod:** one word list, a rule per line: words, cascading timers and
  chances before a ban; `!strike`, `!strikes`, `!pardonplayer`.
- **watchdog** (health, watch mode) and **eventlog**. The watchdog restarts at
  once when the server drops most of its players in one go (`Host roster lost …`
  in `server.log`), without a countdown when nobody is left.
- Core: one RCON connection per server on the host's reserved spot, live server
  state, `server.log` and `status.json` watching, event bus, SQLite records of
  players and actions. Each part runs on its own queue with time limits and is
  switched off alone if it keeps failing.
- **Updates from GitHub releases:** it looks for a new version at start and
  every few hours, says what's new in its window, and installs it only when
  the host types `update` there or runs `start.cmd update` (or, with
  `[updates] install_on_server_restart`, when a server restarts). Servers keep
  running while it updates; only the program files are replaced.
- Commands: `start`, `check`, `stop`, `update`, `rcon`, `init`. Typed in the
  supervisor's window: `update`, `check`, `status`, `stop`, `help`.
