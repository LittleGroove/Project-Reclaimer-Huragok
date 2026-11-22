# Archived tools

Scripts are kept forever; these no longer have anything to work on in Huragok.

- `gen_gameoptions.py`, `reprobe_gameoptions.py`, `game-options-probe.txt` (moved from `tests/data/`): they probed the
  server program's game options and generated `huragok/games/halo3_options_data.py`, the option catalogue behind the
  window's option editor. Huragok 0.7.0 no longer builds or edits playlists, custom games or game options, so the
  editor and the catalogue were taken out. The scripts still name their old paths (`tests/data/game-options-probe.txt`,
  `huragok/games/halo3_options_data.py`).
- `starter-packages/` (moved from `huragok/templates/packages/`): the eight starter packages (Team Slayer, Big Team
  Battle, MLG Hardcore, Infection, Scripted Modes, Campaign Co-op, ODST Firefight, Every Halo), each a `package.toml`
  and its playlist, that the first start copied into `PACKAGES/` for N and `new --package`. Huragok 0.7.0 makes a new
  server with the program's own `dedicated init` and nothing more (plan-0.7.0.md, answer 6), so packages
  (`content/packages.py`, G → 5 "Save as a package", `start.cmd package`) were taken out. Kept here as our starter
  playlists: a host can still copy one into a server folder by hand.
