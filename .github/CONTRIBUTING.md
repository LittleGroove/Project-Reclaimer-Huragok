# Contributing

Everything the supervisor does ships together, so new commands and new parts
come in as pull requests. [docs/development.md](../docs/development.md) explains
how the program is put together, how to add a command or a part, everything a
part can use, and the events it can react to.

Before opening a pull request:

```sh
python -m venv .venv
.venv/Scripts/pip install -e "engine[test]"      # Linux: .venv/bin/pip
.venv/Scripts/python -m pytest engine
```

The tests use a fake server, so they need no game. They also fail if two parts
claim the same chat command or settings section, or if a command has no help
text (it's what `commands.toml` and `!help` show). CI runs them on Windows and Linux.

A few things that keep the supervisor dependable:
- Everything must work the same on Windows and Linux.
- Never retry a refused RCON password, and never resend a moderation command.
- Anything done to a player goes through `ctx.moderation`, so it's recorded.
- A command's `role` is only its default; hosts decide in `commands.toml`.
- New parts are switched off by default unless they're safe for every host.
- Hosts' files (`settings.toml`, `commands.toml`, `automod.txt`) are written
  once and never replaced; the program lives in `engine/`.
- Keep slurs out of the repository: tests use stand-in words.
