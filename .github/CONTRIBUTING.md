# Contributing

Everything Huragok does ships together, so new commands and new parts
come in as pull requests. [docs/development.md](../docs/development.md) explains
how the program is put together (laid out by its three named parts: Huragok, the
Superintendent and Vergil, each layer using only the ones below it), how to add a
command or a part, everything a part can use, and the events it can react to.

Before opening a pull request:

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine -q > tests.txt 2>&1
```

The tests use a fake server, so they need no game, but they start real
processes (the fake `dedicated`, the start scripts) and take about four
minutes (`-m "not slow"` leaves the real-process ones out of a quick run);
[docs/development.md](../docs/development.md#running-the-tests) says more. They
never leave this machine or touch a real server's ports, and a test that tries
fails, as does one that lets an error pass unseen (an asyncio task's, a
destructor's, a coroutine never awaited).
They're laid out like the package, one folder per layer (`engine/tests/README.md`).
They also fail if a module imports one from a layer above it, if two parts claim
the same chat command or settings section, or if a command has no help text (it's what `commands.toml` and `!help` show). CI runs
them on Windows and Linux with Python 3.11 to 3.14, and again from the release zip
(`git archive`), so add every new file to git: one that isn't committed is missing
from the zip hosts install.

A few things that keep Huragok dependable:
- Huragok is run on Windows 11; keep everything working on Linux too (it's
  experimental there, and CI tests it).
- Never retry a refused RCON password on a whim (five wrong ones lock every tool
  on the machine out), and never resend a moderation command.
- Anything done to a player goes through `ctx.moderation`, so it's recorded.
- Nothing blocks the event loop: slow work goes to `asyncio.to_thread`.
- A command's `role` is only its default; hosts decide in `commands.toml`.
- New parts are switched off by default unless they're safe for every host.
- Huragok is installed on hosts' machines, and every update goes over an existing
  install: no release may need a clean install. Hosts' files (`settings.toml`,
  `commands.toml`, `automod.txt`, `names.txt`, `intervalmessages.txt`, `PACKAGES/`,
  `records/`) are theirs and
  are never rewritten: the program lives in `engine/`, and an update replaces only
  that. Read every form of a host's file that any version from 0.4.0 on wrote, and
  when what a file should say changes, add a step to `engine/huragok/updates/hostfiles.py` that brings an
  existing file up to date, with a test from `tests/data/host-0.4.0`
  (docs/development.md, "Hosts' files across updates"). A change to a host's file
  goes through `tomledit`, which writes only what reads back as exactly that change.
- **A beta is always a GitHub pre-release** (`gh release create … --prerelease`), tagged
  with its pre-release version (`v0.5.1-beta.1`, the same as `__version__`). That mark
  is the only thing keeping a beta from hosts with `[updates] beta` off and from every
  Huragok up to 0.4.4. The Release check workflow marks a beta published without it and
  fails, and Huragok 0.5.0 on refuses one with beta off, but neither reaches 0.4.x hosts
  in time: never publish a beta as a full release.
- Never a bare `asyncio.ensure_future` for work nobody waits on: `core/background.py`
  keeps it and says what failed. What's sent to several servers goes through each
  server's queue (`players/outbox.py`), so a frozen server holds up only itself.
- Keep slurs out of the repository: tests use stand-in words.
- No personal details in the repository (names, addresses, player IDs, passwords,
  your own paths): use `C:\reclaimer`, Alice and Bravo, `abcd…` IDs.
