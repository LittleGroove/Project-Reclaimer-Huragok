# Contributing

Everything Huragok does ships together, so new commands and new parts
come in as pull requests. [docs/development.md](../docs/development.md) explains
how the program is put together, how to add a command or a part, everything a
part can use, and the events it can react to.

Before opening a pull request:

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine -q > tests.txt 2>&1
```

The tests use a fake server, so they need no game, but they start real
processes (the fake `dedicated`, the start scripts) and take about three
minutes (`-m "not slow"` leaves the real-process ones out of a quick run);
[docs/development.md](../docs/development.md#running-the-tests) says more. They
never leave this machine or touch a real server's ports, and a test that tries
fails, as does one that lets an error pass unseen (an asyncio task's, a
destructor's, a coroutine never awaited).
They also fail if two parts claim the same chat command or settings section, or if
a command has no help text (it's what `commands.toml` and `!help` show). CI runs
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
- Hosts' files (`settings.toml`, `commands.toml`, `automod.txt`, `names.txt`) are
  written once and never replaced; the program lives in `engine/`. A change to a
  host's file goes through `tomledit`, which writes only what reads back as exactly
  that change.
- Keep slurs out of the repository: tests use stand-in words.
- No personal details in the repository (names, addresses, player IDs, passwords,
  your own paths): use `C:\reclaimer`, Alice and Bravo, `abcd…` IDs.
