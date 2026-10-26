# Huragok's tests

Laid out like the package (`engine/huragok`): each folder tests the layer of the same name, and each file the
module it's named for. What several tests share (the fakes of the server program and its console, folders to run
on, the window driven by a scripted keyboard) is in `testkit/`, never in another test file.

| Folder | |
| --- | --- |
| `architecture/` | How the engine is put together: the layers lore.py's named parts make (`test_layers.py`), the modules' own rules and this folder's layout (`test_modules.py`), and the guards every test runs inside (`test_guards.py`) |
| `core/` | settings.toml, each server's settings in force (`test_serversettings.py`), commands.toml, TOML editing, the mailbox, the part system, process priority, log lines, the text and file helpers every layer shares (`test_text.py`) |
| `games/` | What each game is (`test_game_rules.py`), Halo 3's custom games and map files, ODST |
| `program/` | The server program's own commands, run for a server folder; where a server's folder settings point |
| `servers/` | The Superintendent's: one server's state machine and the fleet (`test_server.py`), the backends, the watchdog, what a server's logs say |
| `players/` | Vergil's: automod, the in-game commands, vote kicks, the relay, interval messages |
| `content/` | New servers, the content and Workshop screens, game files, mods, clean-up |
| `updates/` | Huragok's own updates and the server program's, the Workshop's, the host's files brought up to date, and what a 0.4.x Huragok asks of the version it updates to (`test_from_0_4.py`) |
| `window/` | The dashboard and its screens |
| `commandline/` | start.cmd's commands, the start loop, the lock |
| `testkit/` | What the tests share. Nothing in it is a test |
| this folder | The three named parts running together (`test_huragok.py`), lore.py, each server's console link (`test_rcon.py`), and `test_surfaces.py`: every message the main flows don't reach, provoked and its words checked |

Run them from `engine/`: `python -m pytest -q` (everything), `-m "not slow"` (no real processes, a few minutes
less), or one folder: `python -m pytest tests/servers`. Write the output to a file, as `docs/development.md` says.
`conftest.py` holds the guards every test runs inside: no network, no real server's port, nothing of this PC's
Steam or MCC.
